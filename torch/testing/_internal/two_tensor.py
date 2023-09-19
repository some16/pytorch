import torch
import torch.utils._pytree as pytree
from torch.utils._python_dispatch import return_and_correct_aliasing


# A simple tensor subclass that holds two tensors internally, and runs every op on both tensors.
class TwoTensor(torch.Tensor):
    @staticmethod
    def __new__(cls, a, b):
        assert (
            a.device == b.device
            and a.layout == b.layout
            and a.requires_grad == b.requires_grad
            and a.dtype == b.dtype
        )
        # I guess it would be more accurate to represent the shape as torch.cat(a, b).shape
        shape = a.shape
        kwargs = {}
        kwargs["strides"] = a.stride()
        kwargs["storage_offset"] = a.storage_offset()
        kwargs["device"] = a.device
        kwargs["layout"] = a.layout
        kwargs["requires_grad"] = a.requires_grad
        kwargs["dtype"] = a.dtype
        out = torch.Tensor._make_wrapper_subclass(cls, shape, **kwargs)

        # If a and b are non-contiguous (or have weird storage offsets, etc)
        # Then we want to set the metadata on our wrapper properly too.
        assert a.shape == b.shape
        assert a.stride() == b.stride()
        assert a.storage_offset() == b.storage_offset()
        if (
            out.shape != a.shape
            or out.stride() != a.stride()
            or out.storage_offset() != a.storage_offset()
        ):
            with torch.utils._mode_utils.no_dispatch():
                import pdb; pdb.set_trace()
                out.as_strided_(a.shape, a.stride(), a.storage_offset())
        return out

    def __init__(self, a, b):
        self.a = a
        self.b = b

    def __repr__(self):
        a_repr = repr(self.a)
        b_repr = repr(self.b)
        return f"TwoTensor({a_repr}, {b_repr})"

    def __tensor_flatten__(self):
        return ["a", "b"], None

    @staticmethod
    def __tensor_unflatten__(inner_tensors, meta):
        assert meta is None
        a, b = inner_tensors["a"], inner_tensors["b"]
        return TwoTensor(a, b)

    @classmethod
    def __torch_dispatch__(cls, func, types, args, kwargs):
        if kwargs is None:
            kwargs = {}
        args_a = pytree.tree_map_only(TwoTensor, lambda x: x.a, args)
        args_b = pytree.tree_map_only(TwoTensor, lambda x: x.b, args)

        kwargs_a = pytree.tree_map_only(TwoTensor, lambda x: x.a, kwargs)
        kwargs_b = pytree.tree_map_only(TwoTensor, lambda x: x.b, kwargs)

        out_a = func(*args_a, **kwargs_a)
        out_b = func(*args_b, **kwargs_b)
        assert type(out_a) == type(out_b)
        out_a_flat, spec = pytree.tree_flatten(out_a)
        out_b_flat, _ = pytree.tree_flatten(out_b)
        # for aten ops that return non-tensors, just assume that
        # our two inner tensors return the same value
        out_flat = [
            TwoTensor(o_a, o_b) if isinstance(o_a, torch.Tensor) else o_a
            for o_a, o_b in zip(out_a_flat, out_b_flat)
        ]
        out = pytree.tree_unflatten(out_flat, spec)
        return return_and_correct_aliasing(func, args, kwargs, out)
