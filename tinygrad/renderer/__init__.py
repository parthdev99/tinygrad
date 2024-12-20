from __future__ import annotations
from typing import Optional, Callable, Any
import functools
from dataclasses import dataclass, field, replace
from tinygrad.helpers import to_function_name, dedup, prod
from tinygrad.ops import Ops, UOp, sym_infer, sint, Variable, ssimplify, GroupOp
from tinygrad.dtype import DType

@dataclass(frozen=True)
class TensorCore: # D = A * B + C, A is (M x K), B is (K x N), C and D are (M x N)
  dims: tuple[int,int,int] # N, M, K
  dtype_in: DType # dtype for A and B
  dtype_out: DType # dtype for C and D
  threads: list[tuple[int,int]] # list of (TC dim,amt) that construct the warp thread structure
  reduce_axes: list[tuple[int,int]] # list of (TC dim,amt) that constructs the shape of the reduce dim
  @property
  def early_upcast_axes(self) -> list[tuple[int,int]]: # list of (TC dim,amt) that upcasts the threads remainders of dims [0,1]
    return [(d,self.dims[d]//sz) for d,sz in [(dim,prod(sz for d,sz in self.threads if d==dim)) for dim in range(2)] if self.dims[d]>sz]
  upcast_axes: tuple[list[tuple[int,int]], list[tuple[int,int]], list[tuple[int,int]]] # list of (TC dim,amt) that upcast A, B and C
  st1_pattern: Optional[tuple[tuple[tuple[int,int], ...], tuple[tuple[int,int], ...]]] = None # pattern to fix shapetracker for A
  st2_pattern: Optional[tuple[tuple[tuple[int,int], ...], tuple[tuple[int,int], ...]]] = None # pattern to fix shapetracker for B
  expanded_shape: Optional[tuple[int, ...]] = None
  opts_seq: tuple[str,str] = ("UP","LC") # upcast input, local the thread pattern
  def __str__(self): return "_".join(["WMMA"] + list(map(str, self.dims)) + [self.dtype_in.name, self.dtype_out.name])

@dataclass(frozen=True)
class Estimates:
  # number of FLOPS used in the Kernel
  ops:sint = 0
  # bytes accessed in loads and stores
  lds:sint = 0
  # total bytes accessed, counting only once for bytes that are accessed multiple times
  mem:sint = 0
  def __add__(self, o:Estimates): return Estimates(self.ops + o.ops, self.lds + o.lds, self.mem + o.mem)
  def simplify(self): return Estimates(ssimplify(self.ops), ssimplify(self.lds), ssimplify(self.mem))
  @staticmethod
  def from_uops(uops:list[UOp], ignore_indexing=False) -> Estimates:
    flops: sint = 0
    lds: sint = 0
    mults: sint = 1
    mult_stack: list[sint] = []
    dont_count: set[UOp] = set()
    if ignore_indexing:
      for u in uops:
        if u.op in {Ops.LOAD, Ops.STORE}:
          dont_count = dont_count.union(u.src[0].toposort)
          if len(u.src) > 2: dont_count = dont_count.union(u.src[2].toposort)
        elif u.op is Ops.IF:
          dont_count = dont_count.union(u.src[0].toposort)
    for u in uops:
      if u.op is Ops.RANGE:
        mult_stack.append(mults)
        mults *= (u.src[1] - u.src[0]).ssimplify()
      elif u.op is Ops.ENDRANGE: mults = mult_stack.pop(-1)
      elif u.op is Ops.SPECIAL: mults *= u.arg[1] # NOTE: we don't push to the mult_stack here, you can't end these
      elif u.op is Ops.LOAD: lds += u.dtype.itemsize * mults
      elif u.op is Ops.STORE: lds += u.src[1].dtype.itemsize * mults
      elif u.op in GroupOp.ALU and u not in dont_count: flops += (mults * (2 if u.op is Ops.MULACC else 1)) * u.dtype.count
      elif u.op is Ops.WMMA and u not in dont_count: flops += 2 * prod(u.arg[1]) // u.arg[5] * mults
    return Estimates(flops, lds, lds) # TODO: properly track memory, lds is always a high estimate

@dataclass
class ProgramSpec:
  name:str
  src:str
  device:str
  uops:Optional[list[UOp]]=None
  mem_estimate:sint=0  # TODO: get this from the load/store uops once min/max are good

  # filled in from uops (if we have uops)
  global_size:Optional[list[int]]=None
  local_size:Optional[list[int]]=None
  vars:list[Variable]=field(default_factory=list)
  globals:list[int]=field(default_factory=list)
  outs:list[int]=field(default_factory=list)
  _ran_post_init:bool=False  # NOTE: this is needed if you call replace on the Program

  def __post_init__(self):
    if not self._ran_post_init and self.uops is not None:
      # single pass through the uops
      for u in self.uops:
        if u.op is Ops.DEFINE_VAR: self.vars.append(u)
        if u.op is Ops.DEFINE_GLOBAL: self.globals.append(u.arg)
        if u.op is Ops.STORE: self.outs.extend([x.arg for x in u.src[0].toposort if x.op is Ops.DEFINE_GLOBAL])
        if u.op is Ops.SPECIAL:
          # NOTE: you have to set local_size and global_size to the base [1,1,1] outside this
          if u.arg[0][0] == 'i': self.local_size = None
          special_size = self.local_size if u.arg[0][0] == 'l' else self.global_size
          assert special_size is not None
          special_size[int(u.arg[0][-1])] = u.arg[1]
      self.vars = sorted(self.vars, key=lambda v: v.arg)
      self.outs = sorted(dedup(self.outs))
      self._ran_post_init = True

  @functools.cached_property
  def estimates(self) -> Estimates:
    return replace(Estimates() if self.uops is None else Estimates.from_uops(self.uops, ignore_indexing=True), mem=self.mem_estimate)

  @functools.cached_property
  def function_name(self) -> str: return to_function_name(self.name)

  def launch_dims(self, var_vals:dict[Variable, int]):
    global_size = [sym_infer(sz, var_vals) for sz in self.global_size] if self.global_size is not None else None
    local_size = [sym_infer(sz, var_vals) for sz in self.local_size] if self.local_size is not None else None
    return global_size, local_size

class Renderer:
  device: str = ""
  suffix: str = ""
  # TODO: make this generic with a list of supported types
  supports_float4: bool = True
  has_local: bool = True
  has_shared: bool = True
  # NOTE: these two should be in (x,y,z) order to match the max_sizes argument in get_grouped_dims
  global_max: Optional[tuple[int, ...]] = (0x8FFFFFFF,) * (3) # TODO: UOps.SPECIAL int32 indexes right now
  local_max: Optional[tuple[int, ...]] = (0x8FFFFFFF,) * (3) # TODO: UOps.SPECIAL int32 indexes right now
  shared_max: int = 32768
  tensor_cores: list[TensorCore] = []
  extra_matcher: Any = None
  code_for_op: dict[Ops, Callable] = {}

  def __reduce__(self): return self.__class__, ()
  def render(self, name:str, uops:list[UOp]) -> str: raise NotImplementedError("needs a renderer")
