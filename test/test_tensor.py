import subprocess
import numpy as np
import torch
import unittest, copy, mmap, random, math, array
from tinygrad import Tensor, Device, dtypes
from tinygrad.engine.schedule import create_schedule
from tinygrad.helpers import getenv, temp, _METADATA, mv_address
from extra.gradcheck import numerical_jacobian, jacobian, gradcheck
from hypothesis import given, settings, strategies as strat
from tinygrad.device import is_dtype_supported

settings.register_profile("my_profile", max_examples=200, deadline=None, derandomize=getenv("DERANDOMIZE_CI", False))
settings.load_profile("my_profile")

x_init = np.random.randn(1,3).astype(np.float32)
U_init = np.random.randn(3,3).astype(np.float32)
V_init = np.random.randn(3,3).astype(np.float32)
W_init = np.random.randn(3,3).astype(np.float32)
m_init = np.random.randn(1,3).astype(np.float32)
gradient = np.random.randn(1,3).astype(np.float32)

class TestTinygrad(unittest.TestCase):
  def test_zerodim_initialization(self):
    self.assertEqual(Tensor(55).shape, ())
    self.assertEqual(Tensor(3.14).shape, ())

  def test_plus_equals(self):
    a = Tensor.randn(10,10)
    b = Tensor.randn(10,10)
    c = a + b
    val1 = c.numpy()
    a += b
    val2 = a.numpy()
    np.testing.assert_allclose(val1, val2)

  def test_backward_pass(self):
    def test_tinygrad():
      x = Tensor(x_init, requires_grad=True)
      W = Tensor(W_init, requires_grad=True)
      m = Tensor(m_init)
      out = x.dot(W).relu()
      out = out.log_softmax()
      out = out.mul(m).add(m).sum()
      out.backward()
      return out.numpy(), x.grad.numpy(), W.grad.numpy()

    def test_pytorch():
      x = torch.tensor(x_init, requires_grad=True)
      W = torch.tensor(W_init, requires_grad=True)
      m = torch.tensor(m_init)
      out = x.matmul(W).relu()
      out = torch.nn.functional.log_softmax(out, dim=1)
      out = out.mul(m).add(m).sum()
      out.backward()
      return out.detach().numpy(), x.grad, W.grad

    for x,y in zip(test_tinygrad(), test_pytorch()):
      np.testing.assert_allclose(x, y, atol=1e-5)

  # A simple test is to check that we can accumulate gradients (run backward twice or more times)
  # This will only work if retain_graph works.
  def test_retain_graph(self):
    x = Tensor(x_init, requires_grad=True)
    W = Tensor(W_init, requires_grad=True)
    m = Tensor(m_init)
    out = x.dot(W).relu()
    out = out.log_softmax()
    out = out.mul(m).add(m).sum()
    out.backward(retain_graph=True)
    xgrad,wgrad = x.grad.numpy(), W.grad.numpy()
    out.backward(retain_graph=True)
    xgrad2,wgrad2 = x.grad.numpy(), W.grad.numpy()
    out.backward() # no need to retain again since we will not re-run backward
    xgrad3,wgrad3 = x.grad.numpy(), W.grad.numpy()
    np.testing.assert_allclose(xgrad3, xgrad * 3., atol=1e-6)
    np.testing.assert_allclose(wgrad3, wgrad * 3., atol=1e-6)
    np.testing.assert_allclose(xgrad2, xgrad * 2., atol=1e-6)
    np.testing.assert_allclose(wgrad2, wgrad * 2., atol=1e-6)

  @unittest.expectedFailure
  def test_second_order_backward_pass(self):
    def test_pytorch():
      x = torch.tensor(x_init)
      m = torch.tensor(m_init, requires_grad=True)
      out = x.mul(m).sum()
      # use retain graph so we can compute second order derivatives later
      out.backward(retain_graph=True)
      # save first-order gradient (dO/dm). they still contain graph information on how they were constructed wrt x and W
      grad_m = m.grad
      # zero gradients so second-order gradients are correct
      m.grad = None
      # compute second-order gradients
      grad_m.sum().backward(retain_graph=True)

      # d2O/dm2
      second_grad_m = m.grad
      return second_grad_m.numpy()

    def test_tinygrad():
      x = Tensor(x_init)
      m = Tensor(m_init, requires_grad=True)
      out = x.mul(m).sum()
      out.backward()
      grad_m = m.grad
      m.grad = None
      grad_m.sum().backward()
      second_grad_m = m.grad # currently, this will be None (incorrect)
      return second_grad_m.numpy()

    for x,y in zip(test_tinygrad(), test_pytorch()):
      np.testing.assert_allclose(x, y, atol=1e-5)

  # passing `gradient` to backward
  def test_backward_pass_vjp(self):
    def test_tinygrad():
      x = Tensor(x_init, requires_grad=True)
      W = Tensor(W_init, requires_grad=True)
      m = Tensor(m_init)
      out = x.dot(W).relu()
      out = out.log_softmax()
      out = out.mul(m).add(m)
      out.backward(Tensor(gradient))
      return out.numpy(), x.grad.numpy(), W.grad.numpy()

    def test_pytorch():
      x = torch.tensor(x_init, requires_grad=True)
      W = torch.tensor(W_init, requires_grad=True)
      m = torch.tensor(m_init)
      out = x.matmul(W).relu()
      out = torch.nn.functional.log_softmax(out, dim=1)
      out = out.mul(m).add(m)
      out.backward(torch.tensor(gradient))
      return out.detach().numpy(), x.grad, W.grad

    for x,y in zip(test_tinygrad(), test_pytorch()):
      np.testing.assert_allclose(x, y, atol=1e-5)

  def test_backward_pass_diamond_model(self):
    def test_tinygrad():
      u = Tensor(U_init, requires_grad=True)
      v = Tensor(V_init, requires_grad=True)
      w = Tensor(W_init, requires_grad=True)
      x = u.mul(v).relu()
      y = u.mul(w).relu()
      out = x.add(y).mul(y).relu()
      out = out.log_softmax()
      out = out.sum()
      out.backward()
      return out.numpy(), u.grad.numpy(), v.grad.numpy(), w.grad.numpy()

    def test_pytorch():
      u = torch.tensor(U_init, requires_grad=True)
      v = torch.tensor(V_init, requires_grad=True)
      w = torch.tensor(W_init, requires_grad=True)
      x = u.mul(v).relu()
      y = u.mul(w).relu()
      out = x.add(y).mul(y).relu()
      out = torch.nn.functional.log_softmax(out, dim=1)
      out = out.sum()
      out.backward()
      return out.detach().numpy(), u.grad, v.grad, w.grad

    for x,y in zip(test_tinygrad(), test_pytorch()):
      np.testing.assert_allclose(x, y, atol=1e-5, rtol=1e-6)

  def test_nograd(self):
    x = Tensor(x_init, requires_grad=False)
    m = Tensor(m_init, requires_grad=False)
    W = Tensor(W_init, requires_grad=True)
    tmp = x.mul(m)
    mm = tmp.matmul(W)
    out = mm.relu()
    out = out.sum()
    out.backward()
    assert x.grad is None
    assert m.grad is None
    assert tmp.grad is None
    assert mm.grad is not None
    assert W.grad is not None

  def test_dropout(self):
    with Tensor.train():
      n, rate = 1_000_000, 0.1
      w = Tensor.ones(n).dropout(rate)
      non_zeros = np.count_nonzero(w.numpy())
      expected = n * (1 - rate)
      np.testing.assert_allclose(non_zeros, expected, rtol=2e-3)

  def test_jacobian(self):
    W = np.random.RandomState(42069).random((10, 5)).astype(np.float32)
    x = np.random.RandomState(69420).random((1, 10)).astype(np.float32)

    torch_x = torch.tensor(x, requires_grad=True)
    torch_W = torch.tensor(W, requires_grad=True)
    def torch_func(x): return torch.nn.functional.log_softmax(x.matmul(torch_W).relu(), dim=1)
    PJ = torch.autograd.functional.jacobian(torch_func, torch_x).squeeze().numpy()

    tiny_x = Tensor(x, requires_grad=True)
    tiny_W = Tensor(W, requires_grad=True)
    def tiny_func(x): return x.dot(tiny_W).relu().log_softmax()
    J = jacobian(tiny_func, tiny_x)
    NJ = numerical_jacobian(tiny_func, tiny_x)

    np.testing.assert_allclose(PJ, J, atol = 1e-5)
    np.testing.assert_allclose(PJ, NJ, atol = 1e-3)

  def test_gradcheck(self):
    W = np.random.RandomState(1337).random((10, 5)).astype(np.float32)
    x = np.random.RandomState(7331).random((1, 10)).astype(np.float32)

    tiny_x = Tensor(x, requires_grad=True)
    tiny_W = Tensor(W, requires_grad=True)
    def tiny_func(x): return x.dot(tiny_W).relu().log_softmax()

    self.assertTrue(gradcheck(tiny_func, tiny_x, eps = 1e-3))

    # coarse approx. since a "big" eps and the non-linearities of the model
    self.assertFalse(gradcheck(tiny_func, tiny_x, eps = 1e-5))

  def test_random_fns_are_deterministic_with_seed(self):
    for random_fn in [Tensor.randn, Tensor.normal, Tensor.uniform, Tensor.scaled_uniform, Tensor.glorot_uniform, Tensor.kaiming_normal]:
      with self.subTest(msg=f"Tensor.{random_fn.__name__}"):
        Tensor.manual_seed(1337)
        a = random_fn(10,10).realize()
        Tensor.manual_seed(1337)
        b = random_fn(10,10).realize()
        np.testing.assert_allclose(a.numpy(), b.numpy())

  def test_randn_isnt_inf_on_zero(self):
    # simulate failure case of rand handing a zero to randn
    original_rand, Tensor.rand = Tensor.rand, Tensor.zeros
    try: self.assertNotIn(np.inf, Tensor.randn(16).numpy())
    except: raise
    finally: Tensor.rand = original_rand

  def test_zeros_like_has_same_dtype_and_shape(self):
    for datatype in [dtypes.float16, dtypes.float32, dtypes.int8, dtypes.int32, dtypes.int64, dtypes.uint8]:
      a = Tensor([1, 2, 3], dtype=datatype)
      b = Tensor.zeros_like(a)
      assert a.dtype == b.dtype, f"dtype mismatch {a.dtype=} != {b.dtype}"
      assert a.shape == b.shape, f"shape mismatch {a.shape} != {b.shape}"

    a = Tensor([1, 2, 3])
    b = Tensor.zeros_like(a, dtype=dtypes.int8)
    assert a.dtype == dtypes.default_int and b.dtype == dtypes.int8, "a.dtype should be int and b.dtype should be char"
    assert a.shape == b.shape, f"shape mismatch {a.shape} != {b.shape}"

  def test_ones_like_has_same_dtype_and_shape(self):
    for datatype in [dtypes.float16, dtypes.float32, dtypes.int8, dtypes.int32, dtypes.int64, dtypes.uint8]:
      a = Tensor([1, 2, 3], dtype=datatype)
      b = Tensor.ones_like(a)
      assert a.dtype == b.dtype, f"dtype mismatch {a.dtype=} != {b.dtype}"
      assert a.shape == b.shape, f"shape mismatch {a.shape} != {b.shape}"

    a = Tensor([1, 2, 3])
    b = Tensor.ones_like(a, dtype=dtypes.int8)
    assert a.dtype == dtypes.default_int and b.dtype == dtypes.int8, "a.dtype should be int and b.dtype should be char"
    assert a.shape == b.shape, f"shape mismatch {a.shape} != {b.shape}"

  def test_rand_like_device(self):
    a = Tensor.ones(3, 3, device="CLANG")
    b = Tensor.rand_like(a)
    self.assertEqual(b.device, a.device)

  def test_ndim(self):
    assert Tensor(1).ndim == 0
    assert Tensor.randn(1).ndim == 1
    assert Tensor.randn(2,2,2).ndim == 3
    assert Tensor.randn(1,1,1,1,1,1).ndim == 6

  def test_argfix(self):
    for f in [Tensor.zeros, Tensor.ones, Tensor.rand, Tensor.randn, Tensor.empty]:
      self.assertEqual(f().shape, ())
      self.assertEqual(f(1).shape, (1,))
      self.assertEqual(f(10,20,40).shape, (10,20,40))
      self.assertEqual(f([]).shape, ())
      self.assertEqual(f([1]).shape, (1,))
      self.assertEqual(f([10,20,40]).shape, (10,20,40))
      self.assertEqual(f(()).shape, ())
      self.assertEqual(f((1,)).shape, (1,))
      self.assertEqual(f((10,20,40)).shape, (10,20,40))

      with self.assertRaises(ValueError): f((2, 2), 2, 2)
      with self.assertRaises(ValueError): f((2, 2), (2, 2))
      with self.assertRaises(ValueError): f((128, 128), 0.0, 0.01)

  def test_numel(self):
    assert Tensor.randn(10, 10).numel() == 100
    assert Tensor.randn(1,2,5).numel() == 10
    assert Tensor.randn(1,1,1,1,1,1).numel() == 1
    assert Tensor([]).numel() == 0
    assert Tensor.randn(1,0,2,5).numel() == 0
    assert Tensor(3).numel() == 1

  def test_len(self):
    assert len(torch.zeros(7)) == len(Tensor.zeros(7))
    assert len(torch.zeros(10,20)) == len(Tensor.zeros(10,20))
    assert len(torch.zeros(10,20)) == len(Tensor.zeros(10,20,30))
    assert len(torch.zeros(1).flatten()) == len(Tensor.zeros(1).flatten())
    with self.assertRaises(TypeError): len(Tensor(3))

  def test_size(self):
    t1, t2 = torch.zeros(10,20), Tensor.zeros(10,20)
    assert t1.size() == t2.size()
    assert t1.size(0) == t2.size(0)
    assert t1.size(1) == t2.size(1)
    assert t1.size(-1) == t2.size(-1)
    assert t1.size(-2) == t2.size(-2)
    with self.assertRaises(IndexError): t2.size(2)

  def test_tolist(self):
    # NOTE: float16 Tensor.tolist() requires python 3.12
    for arr in [[1,2,3], [1.5,2,3], [[1,2,3], [4,5,6]], 3]:
      assert Tensor(arr).tolist() == torch.tensor(arr).tolist() == arr

  def test_element_size(self):
    for _, dtype in dtypes.fields().items():
      assert dtype.itemsize == Tensor.randn(3, dtype=dtype).element_size(), f"Tensor.element_size() not matching Tensor.dtype.itemsize for {dtype}"

  def test_deepwalk_ctx_check(self):
    layer = Tensor.uniform(1, 1, requires_grad=True)
    x = Tensor.randn(1, 1, 1)
    x.dot(layer).mean().backward()
    x = Tensor.randn(1, 1, 1)
    x.dot(layer).mean().backward()

  def test_zerosized_tensors(self):
    np.testing.assert_equal(Tensor([]).numpy(), np.array([]))
    np.testing.assert_equal(Tensor(None).numpy(), np.array([]))

  def test_tensor_ndarray_dtype(self):
    arr = np.array([1]) # where dtype is implicitly int64
    assert Tensor(arr).dtype == dtypes.int64
    assert Tensor(arr, dtype=dtypes.float32).dtype == dtypes.float32 # check if ndarray correctly casts to Tensor dtype
    assert Tensor(arr, dtype=dtypes.float64).dtype == dtypes.float64 # check that it works for something else

  def test_tensor_from_blob(self):
    x = memoryview(bytearray(16)).cast('I')

    t = Tensor.from_blob(mv_address(x), (4,), dtype=dtypes.int, device="CLANG")
    z = (t+1)
    np.testing.assert_equal(z.numpy(), [1, 1, 1, 1])

    x[:] = array.array('I', [0, 1, 2, 3])
    z = (t+1)
    np.testing.assert_equal(z.numpy(), [1, 2, 3, 4])

  def test_tensor_list_dtype(self):
    for arr in ([1], [[[1]]], [[1,1],[1,1]], [[[1,1],[1,1]],[[1,1],[1,1]]]):
      assert Tensor(arr).dtype == dtypes.default_int
      assert Tensor(arr, dtype=dtypes.float32).dtype == dtypes.float32
      assert Tensor(arr, dtype=dtypes.float64).dtype == dtypes.float64

    for arr in ([True], [[[False]]], [[True,False],[True,False]], [[[False,True],[False,False]],[[True,True],[False,True]]]):
      assert Tensor(arr).dtype == dtypes.bool
      assert Tensor(arr, dtype=dtypes.float32).dtype == dtypes.float32
      assert Tensor(arr, dtype=dtypes.float64).dtype == dtypes.float64

    # empty tensor defaults
    for arr in ([], [[[]]], [[],[]]):
      t = Tensor(arr)
      assert t.dtype == dtypes.default_float
      np.testing.assert_allclose(t.numpy(), np.array(arr))

    # mixture of bool and int
    for arr in ([True, 3], [[True],[3]], [[[True]], [[3]]], [[True, 3], [3, True]]):
      t = Tensor(arr)
      assert t.dtype == dtypes.default_int
      np.testing.assert_allclose(t.numpy(), np.array(arr))

    # mixture of bool, int and float
    for arr in ([[True,True],[3.,True]], [[0,1],[3.,4]], [[[0],[1]],[[3.],[4]]], [[[True],[1]],[[3.],[4]]]):
      t = Tensor(arr)
      assert t.dtype == dtypes.default_float
      np.testing.assert_allclose(t.numpy(), np.array(arr))

  def test_tensor_list_shapes(self):
    self.assertEqual(Tensor([[[]]]).shape, (1,1,0))
    self.assertEqual(Tensor([[],[]]).shape, (2,0))
    self.assertEqual(Tensor([[[[]],[[]]], [[[]],[[]]], [[[]],[[]]]]).shape, (3,2,1,0))

  def test_tensor_list_errors(self):
    # inhomogeneous shape
    with self.assertRaises(ValueError): Tensor([[],[[]]])
    with self.assertRaises(ValueError): Tensor([[1],[]])
    with self.assertRaises(ValueError): Tensor([[1],[1],1])
    with self.assertRaises(ValueError): Tensor([[[1,1,1],[1,1]]])
    with self.assertRaises(ValueError): Tensor([[1,1,1],[[1,1,1]]])

  def test_tensor_mixed_list_tuple(self):
    def _list_or_tuple(): return list if random.random() < 0.5 else tuple
    def _generate_data(depth):
      if depth == 0: return _list_or_tuple()()
      if depth == 1: return _list_or_tuple()([random.random(), random.random()])
      return _list_or_tuple()([_generate_data(depth-1), _generate_data(depth-1)])

    for depth in range(7):
      for _ in range(20):
        data = _generate_data(depth)
        np.testing.assert_allclose(Tensor(data).numpy(), np.array(data))

  def test_tensor_list_special_values(self):
    if is_dtype_supported(dtypes.float16):
      data = [math.nan, -math.inf, 65504, 65519, 65519.999, 65520, 65520.1]
      data = data + [-x for x in data]
      with np.errstate(over='ignore'): np.testing.assert_allclose(Tensor(data, dtype=dtypes.float16).numpy(), np.array(data).astype(np.float16))

    # uint32
    data = [1 << 33, 1 << 32, 1 << 32 - 1, 1]
    data = data + [-x for x in data]
    np.testing.assert_allclose(Tensor(data, dtype=dtypes.uint32).numpy(), np.array(data).astype(np.uint32))

    # int32
    data = [1 << 33, 1 << 32, 1 << 32 - 1, 1]
    data = data + [-x for x in data]
    np.testing.assert_allclose(Tensor(data, dtype=dtypes.int32).numpy(), np.array(data).astype(np.int32))

  def test_tensor_list_ndarray(self):
    data = [np.array([1, 2, 3]), np.array([1, 2, 3]), np.array([1, 2, 3])]
    np.testing.assert_equal(Tensor(data).numpy(), np.array(data))
    data = [np.array([1.0, 2.0, 3.0]), np.array([1, 2, 3]), np.array([1, 2, 3])]
    np.testing.assert_equal(Tensor(data).numpy(), np.array(data))
    data = [np.array(1.0), np.array(2.0), np.array(3.0)]
    np.testing.assert_equal(Tensor(data).numpy(), np.array(data))

  def test_tensor_dtype_errors(self):
    with self.assertRaises(AttributeError): Tensor([3], dtype="typo")
    with self.assertRaises(TypeError): Tensor([3], dtype=(dtypes.int,))

  def test_tensor_bytes(self):
    data = b"abc123"
    t = Tensor(data)
    assert t.dtype == dtypes.uint8
    assert t.shape == (6,)
    np.testing.assert_equal(t.numpy(), list(data))

  def test_tensor_copy(self):
    x = copy.deepcopy(Tensor.ones((3,3,3)))
    np.testing.assert_allclose(x.numpy(), np.ones((3,3,3)))

  def test_copy_from_disk(self):
    t = Tensor.randn(30).to(f"disk:{temp('test_copy_from_disk')}")
    a = t[10:20]
    dev = a.to(Device.DEFAULT)
    np.testing.assert_allclose(a.numpy(), dev.numpy())

  # Regression test for https://github.com/tinygrad/tinygrad/issues/1751
  def test_copy_from_numpy_unaligned(self):
    # 2**15 is the minimum for repro
    arr = np.random.randn(2**15).astype(np.float32)
    fn = temp('test_copy_from_numpy_unaligned')
    with open(fn, 'wb') as f: f.write(b't' + arr.tobytes())
    with open(fn, "a+b") as f: memview = memoryview(mmap.mmap(f.fileno(), arr.nbytes + 1))
    ua_arr = np.frombuffer(memview[1:], dtype=arr.dtype, count=arr.shape[0])
    np.testing.assert_allclose(arr, ua_arr)
    assert not ua_arr.flags.aligned
    # force device copy - to() is opt'd away - Tensor(dev)/1 is ignored
    np.testing.assert_allclose(ua_arr, (Tensor(ua_arr)/Tensor(1)).numpy())

  def test_item_to_tensor_to_item(self):
    for a in [0, 1, 2, 3, -1, -100, 100, -101.1, 2.345, 100.1, True, False]:
      item = Tensor(a).item()
      assert type(item) is type(a), a
      np.testing.assert_allclose(item, a), a
      buffered_item = Tensor([a]).item()
      assert type(buffered_item) is type(a), a
      np.testing.assert_allclose(buffered_item, a), a
      reshaped_item = Tensor([a]).reshape((1, 1, 1, 1, 1)).item()
      assert type(reshaped_item) is type(a), a
      np.testing.assert_allclose(reshaped_item, a), a

  def test_no_bool(self):
    with self.assertRaises(TypeError):
      if Tensor(3):
        print("hi")

    with self.assertRaises(TypeError):
      _a = Tensor([3]) in [Tensor([3]), Tensor([4]), Tensor([5])]

  def test_repr_with_grad(self):
    a = Tensor([1], requires_grad=True)
    b = Tensor([1])
    c = (a + b).mean().backward()
    print(a)
    print(c)

  def test_env_overwrite_default_device(self):
    subprocess.run(['DISK=1 python3 -c "from tinygrad import Device; assert Device.DEFAULT != \\"DISK\\""'],
                    shell=True, check=True)
    subprocess.run(['NPY=1 python3 -c "from tinygrad import Device; assert Device.DEFAULT != \\"NPY\\""'],
                    shell=True, check=True)
    subprocess.run([f'{Device.DEFAULT}=1 python3 -c "from tinygrad import Device; assert Device.DEFAULT == \\"{Device.DEFAULT}\\""'],
                    shell=True, check=True)
    subprocess.run([f'DISK=1 {Device.DEFAULT}=1 python3 -c "from tinygrad import Device; assert Device.DEFAULT == \\"{Device.DEFAULT}\\""'],
                    shell=True, check=True)
    subprocess.run([f'NPY=1 {Device.DEFAULT}=1 python3 -c "from tinygrad import Device; assert Device.DEFAULT == \\"{Device.DEFAULT}\\""'],
                    shell=True, check=True)

@unittest.skip("this test is just flaky, sync issue")
class TestMoveTensor(unittest.TestCase):
  d0, d1 = f"{Device.DEFAULT}:0", f"{Device.DEFAULT}:1"
  @given(strat.sampled_from([d0, d1]), strat.sampled_from([d0, d1]),
         strat.sampled_from([dtypes.float16, dtypes.float32]), strat.sampled_from([True, False, None]))
  def test_to_preserves(self, src, dest, dtype, requires_grad):
    if not is_dtype_supported(dtype):
      return
    s = Tensor([1, 2, 3], device=src, dtype=dtype, requires_grad=requires_grad)
    if requires_grad: s.sum().backward()
    t = s.to(dest)
    np.testing.assert_equal(s.numpy(), t.numpy())
    assert s.dtype == t.dtype
    assert s.requires_grad == t.requires_grad
    if requires_grad:
      np.testing.assert_equal(s.grad.numpy(), t.grad.numpy())

  @given(strat.sampled_from([dtypes.float16, dtypes.float32]), strat.sampled_from([True, False, None]))
  def test_shard_preserves(self, dtype, requires_grad):
    s = Tensor([1, 2, 3], dtype=dtype, requires_grad=requires_grad)
    t = s.shard((f"{Device.DEFAULT}:0", f"{Device.DEFAULT}:1"))
    np.testing.assert_equal(s.numpy(), t.numpy())
    assert s.dtype == t.dtype
    assert s.requires_grad == t.requires_grad

  @given(strat.sampled_from([d0, d1]))
  def test_same_dev(self, dev):
    x = Tensor([1,2,3], device=dev)
    y = x.to(dev)
    assert x is y

  def test_to_grad(self):
    x = Tensor.eye(3, requires_grad=True, device=self.d0)
    y = Tensor([[2.0,0,-2.0]], requires_grad=True, device=self.d0)
    z = y.matmul(x).to(self.d1).sum()
    z.backward()
    np.testing.assert_equal(x.grad.numpy(), [[2,2,2],[0,0,0],[-2,-2,-2]])

class TestZeroShapeTensor(unittest.TestCase):
  def test_shape_stride(self):
    t = Tensor.empty(3, 2, 0)
    assert t.shape == (3, 2, 0)
    # numpy has stride 0, 0, 0; torch has stride 2, 1, 1
    assert t.lazydata.st.real_strides() == (0, 0, 0)

    t = Tensor.empty(3, 0, 2)
    assert t.shape == (3, 0, 2)
    # numpy has stride 0, 0, 0; torch has stride 2, 2, 1
    assert t.lazydata.st.real_strides() == (0, 0, 0)

    t = Tensor.empty(0, 0, 0)
    assert t.shape == (0, 0, 0)
    # numpy has stride 0, 0, 0; torch has stride 1, 1, 1
    assert t.lazydata.st.real_strides() == (0, 0, 0)

  def test_rand(self):
    t = Tensor.rand(3, 2, 0)
    assert t.shape == (3, 2, 0)
    np.testing.assert_equal(t.numpy(), np.zeros((3, 2, 0)))
    t = Tensor.rand(0)
    assert t.shape == (0,)
    np.testing.assert_equal(t.numpy(), np.zeros((0,)))
    t = Tensor.rand(0, 0, 0)
    assert t.shape == (0, 0, 0)
    np.testing.assert_equal(t.numpy(), np.zeros((0, 0, 0)))

  def test_full(self):
    t = Tensor.zeros(3, 2, 0)
    assert t.shape == (3, 2, 0)
    np.testing.assert_equal(t.numpy(), np.zeros((3, 2, 0)))
    t = Tensor.full((3, 2, 0), 12)
    assert t.shape == (3, 2, 0)
    np.testing.assert_equal(t.numpy(), np.full((3, 2, 0), 12))

  def test_reshape(self):
    t = Tensor.zeros(3, 2, 0)
    a = t.reshape(7, 0)
    assert a.shape == (7, 0)
    np.testing.assert_equal(a.numpy(), np.zeros((7, 0)))
    a = t.reshape(0)
    assert a.shape == (0,)
    np.testing.assert_equal(a.numpy(), np.zeros((0,)))
    with self.assertRaises(ValueError):
      # cannot reshape from size 0 to size 1
      a = t.reshape(())

  def test_expand(self):
    t = Tensor.full((1, 2, 0), 12).expand((6, 2, 0))
    assert t.shape == (6, 2, 0)
    np.testing.assert_equal(t.numpy(), np.full((6, 2, 0), 12))

  def test_pad(self):
    t = Tensor.rand(3, 2, 0).pad((None, None, (1, 1)), value=1)
    assert t.shape == (3, 2, 2)
    np.testing.assert_equal(t.numpy(), np.ones((3, 2, 2)))

    t = Tensor.rand(3, 2, 0).pad((None, (1, 1), None), value=1)
    assert t.shape == (3, 4, 0)
    np.testing.assert_equal(t.numpy(), np.ones((3, 4, 0)))

    t = Tensor.rand(3, 2, 0).pad(((1, 1), None, None), value=1)
    assert t.shape == (5, 2, 0)
    np.testing.assert_equal(t.numpy(), np.ones((5, 2, 0)))

  def test_shrink_into_zero(self):
    t = Tensor.rand(3, 4).realize()
    assert t.shrink((None, (2, 2))).realize().shape == (3, 0)
    assert t.shrink(((2, 2), None)).realize().shape == (0, 4)
    assert t.shrink(((2, 2), (2, 2))).realize().shape == (0, 0)

  def test_cat(self):
    a = Tensor.rand(3, 2, 2)
    b = Tensor.rand(3, 2, 0)

    t = a.cat(b, dim=2)
    assert t.shape == (3, 2, 2)
    np.testing.assert_equal(t.numpy(), a.numpy())

    t = b.cat(a, dim=2)
    assert t.shape == (3, 2, 2)
    np.testing.assert_equal(t.numpy(), a.numpy())

    t = b.cat(b, dim=0)
    assert t.shape == (6, 2, 0)
    np.testing.assert_equal(t.numpy(), np.zeros((6, 2, 0)))
    t = b.cat(b, dim=1)
    assert t.shape == (3, 4, 0)
    np.testing.assert_equal(t.numpy(), np.zeros((3, 4, 0)))
    t = b.cat(b, dim=2)
    assert t.shape == (3, 2, 0)
    np.testing.assert_equal(t.numpy(), np.zeros((3, 2, 0)))

  def test_elementwise(self):
    a = Tensor.rand(3, 2, 0)
    a_exp = a.exp()
    assert a_exp.shape == (3, 2, 0)
    np.testing.assert_equal(a_exp.numpy(), np.exp(a.numpy()))

    b = Tensor.rand(3, 2, 0)
    assert b.shape == (3, 2, 0)
    ab = a * b
    assert ab.shape == (3, 2, 0)
    np.testing.assert_equal(ab.numpy(), a.numpy() * b.numpy())

    mask = (Tensor.rand(3, 2, 0) > 0.5)
    assert mask.shape == (3, 2, 0)
    c = mask.where(a, b)
    assert c.shape == (3, 2, 0)
    np.testing.assert_equal(c.numpy(), np.where(mask.numpy(), a.numpy(), b.numpy()))

  def test_reduce_over_non_zero(self):
    a = Tensor.ones(3, 2, 0).sum(axis=1)
    assert a.shape == (3, 0)
    np.testing.assert_equal(a.numpy(), np.sum(np.zeros((3, 2, 0)), axis=1))

  def test_reduce_over_zero(self):
    a = Tensor.ones(3, 2, 0).sum(axis=2)
    assert a.shape == (3, 2)
    np.testing.assert_equal(a.numpy(), np.sum(np.zeros((3, 2, 0)), axis=2))

    a = Tensor.ones(3, 2, 0).sum(axis=2, keepdim=True)
    assert a.shape == (3, 2, 1)
    np.testing.assert_equal(a.numpy(), np.sum(np.zeros((3, 2, 0)), axis=2, keepdims=True))

  def test_clone(self):
    a = Tensor.rand(16, 16).realize()
    np.testing.assert_allclose(a.numpy(), a.clone().numpy())

    a = Tensor.rand(16, 16).mul(5.0).add(5.0)
    np.testing.assert_allclose(a.numpy(), a.clone().numpy())

  def test_clone_with_grad(self):
    a = Tensor.rand(16, 16, requires_grad=True)
    a.mul(5.0).add(5.0).mean().backward()
    b = a.clone()
    assert a.grad is not None
    assert b.grad is not None
    np.testing.assert_allclose(a.grad.numpy(), b.grad.numpy())

  def test_reduce_default(self):
    np.testing.assert_equal(Tensor([]).max().numpy(), -float("inf"))
    np.testing.assert_equal(Tensor([]).min().numpy(), float("inf"))
    np.testing.assert_equal(Tensor([]).sum().numpy(), 0)
    np.testing.assert_equal(Tensor([]).mean().numpy(), float("nan"))

class TestTensorCreationDevice(unittest.TestCase):
  # test auxiliary tensors are created on the same device
  def test_one_hot(self):
    y = Tensor([1, 2, 3]).to("CLANG")
    x = y.one_hot(10)
    x.realize()

class TestTrainMode(unittest.TestCase):
  def test_train_mode(self):
    assert not Tensor.training
    @Tensor.train()
    def f():
      assert Tensor.training
    f()
    assert not Tensor.training

class TestInferenceMode(unittest.TestCase):
  def test_inference(self):
    x = Tensor(x_init, requires_grad=True)
    m = Tensor(m_init, requires_grad=True)
    W = Tensor(W_init, requires_grad=True)
    with Tensor.test():
      tmp = x.mul(m)
      mm = tmp.matmul(W)
      out = mm.relu()
      out = out.sum()
      out.backward()
    assert x.grad is None
    assert m.grad is None
    assert tmp.grad is None
    assert mm.grad is None
    assert W.grad is None
    assert W.requires_grad

  def test_no_grad_mode_context_manager(self):
    x = Tensor(x_init, requires_grad=True)
    m = Tensor(m_init, requires_grad=True)
    W = Tensor(W_init, requires_grad=True)
    @Tensor.test()
    def f(x, m, W):
      tmp = x.mul(m)
      mm = tmp.matmul(W)
      out = mm.relu()
      out = out.sum()
      out.backward()
      assert x.grad is None
      assert m.grad is None
      assert tmp.grad is None
      assert mm.grad is None
      assert W.grad is None
    f(x, m, W)

class TestTensorMetadata(unittest.TestCase):
  def setUp(self) -> None: _METADATA.set(None)
  def test_matmul(self):
    x = Tensor.rand(3, requires_grad=True)
    W = Tensor.rand(3, 3, requires_grad=True)
    out = x.matmul(W)
    self.assertEqual(out.lazydata.metadata.name, "matmul")
    si = create_schedule([out.lazydata])[-1]
    self.assertEqual(len(si.metadata), 1)
    self.assertEqual(si.metadata[0].name, "matmul")

  def test_relu(self):
    x = Tensor.rand(3, requires_grad=True)
    out = x.relu()
    self.assertEqual(out.lazydata.metadata.name, "relu")
    si = create_schedule([out.lazydata])[-1]
    self.assertEqual(len(si.metadata), 1)
    self.assertEqual(si.metadata[0].name, "relu")

  def test_complex(self):
    x = Tensor.rand(3, requires_grad=True)
    y = Tensor.rand(3, requires_grad=True)
    out = x.relu() * y.sigmoid()
    self.assertEqual(out.lazydata.metadata.name, "__mul__")
    self.assertEqual(out.lazydata.srcs[0].metadata.name, "relu")
    self.assertEqual(out.lazydata.srcs[1].metadata.name, "sigmoid")
    si = create_schedule([out.lazydata])[-1]
    self.assertEqual(len(si.metadata), 3)
    self.assertEqual(set(m.name for m in si.metadata), {"relu", "sigmoid", "__mul__"})

  def test_complex_backward(self):
    x = Tensor.rand(3, requires_grad=True)
    y = Tensor.rand(3, requires_grad=True)
    out = (x.relu() * y.sigmoid()).sum()
    self.assertEqual(out.lazydata.metadata.name, "sum")
    out.backward()
    self.assertEqual(x.grad.lazydata.metadata.name, "relu")
    self.assertTrue(x.grad.lazydata.metadata.backward)
    self.assertEqual(y.grad.lazydata.metadata.name, "sigmoid")
    self.assertTrue(y.grad.lazydata.metadata.backward)
    si = create_schedule([out.lazydata, x.grad.lazydata, y.grad.lazydata])[-1]
    self.assertEqual(len(si.metadata), 3, f"failed with {si.metadata}")
    self.assertEqual(set(m.name for m in si.metadata), {"sigmoid", "sigmoid", "relu"})
    bw = [m for m in si.metadata if m.backward]
    self.assertEqual(len(bw), 1)
    self.assertEqual(bw[0].name, "sigmoid")

if __name__ == '__main__':
  unittest.main()
