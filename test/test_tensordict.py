# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import os
import re
import uuid

import numpy as np
import pytest
import torch

from tensordict.nn import TensorDictParams

try:
    import torchsnapshot

    _has_torchsnapshot = True
    TORCHSNAPSHOT_ERR = ""
except ImportError as err:
    _has_torchsnapshot = False
    TORCHSNAPSHOT_ERR = str(err)

try:
    import h5py  # noqa

    _has_h5py = True
except ImportError:
    _has_h5py = False

from _utils_internal import decompose, get_available_devices, prod, TestTensorDictsBase
from functorch import dim as ftdim

from tensordict import LazyStackedTensorDict, MemmapTensor, TensorDict
from tensordict.tensordict import (
    _CustomOpTensorDict,
    _stack as stack_td,
    assert_allclose_td,
    dense_stack_tds,
    is_tensor_collection,
    make_tensordict,
    pad,
    pad_sequence,
    SubTensorDict,
    TensorDictBase,
)
from tensordict.utils import _getitem_batch_size, convert_ellipsis_to_idx
from torch import multiprocessing as mp, nn


@pytest.mark.parametrize("device", get_available_devices())
def test_tensordict_set(device):
    torch.manual_seed(1)
    td = TensorDict({}, batch_size=(4, 5), device=device)
    td.set("key1", torch.randn(4, 5))
    assert td.device == torch.device(device)
    # by default inplace:
    with pytest.raises(RuntimeError):
        td.set("key1", torch.randn(5, 5, device=device))

    # robust to dtype casting
    td.set_("key1", torch.ones(4, 5, device=device, dtype=torch.double))
    assert (td.get("key1") == 1).all()

    # robust to device casting
    td.set("key_device", torch.ones(4, 5, device="cpu", dtype=torch.double))
    assert td.get("key_device").device == torch.device(device)

    with pytest.raises(KeyError, match="not found in TensorDict with keys"):
        td.set_("smartypants", torch.ones(4, 5, device="cpu", dtype=torch.double))
    # test set_at_
    td.set("key2", torch.randn(4, 5, 6, device=device))
    x = torch.randn(6, device=device)
    td.set_at_("key2", x, (2, 2))
    assert (td.get("key2")[2, 2] == x).all()

    # test set_at_ with dtype casting
    x = torch.randn(6, dtype=torch.double, device=device)
    td.set_at_("key2", x, (2, 2))  # robust to dtype casting
    torch.testing.assert_close(td.get("key2")[2, 2], x.to(torch.float))

    td.set("key1", torch.zeros(4, 5, dtype=torch.double, device=device), inplace=True)
    assert (td.get("key1") == 0).all()
    td.set(
        "key1",
        torch.randn(4, 5, 1, 2, dtype=torch.double, device=device),
        inplace=False,
    )
    assert td["key1"].shape == td._tensordict["key1"].shape


@pytest.mark.parametrize("device", get_available_devices())
def test_tensordict_device(device):
    tensordict = TensorDict({"a": torch.randn(3, 4)}, [])
    assert tensordict.device is None

    tensordict = TensorDict({"a": torch.randn(3, 4, device=device)}, [])
    assert tensordict["a"].device == device
    assert tensordict.device is None

    tensordict = TensorDict(
        {
            "a": torch.randn(3, 4, device=device),
            "b": torch.randn(3, 4),
            "c": torch.randn(3, 4, device="cpu"),
        },
        [],
        device=device,
    )
    assert tensordict.device == device
    assert tensordict["a"].device == device
    assert tensordict["b"].device == device
    assert tensordict["c"].device == device

    tensordict = TensorDict({}, [], device=device)
    tensordict["a"] = torch.randn(3, 4)
    tensordict["b"] = torch.randn(3, 4, device="cpu")
    assert tensordict["a"].device == device
    assert tensordict["b"].device == device

    tensordict = TensorDict({"a": torch.randn(3, 4)}, [])
    tensordict = tensordict.to(device)
    assert tensordict.device == device
    assert tensordict["a"].device == device


@pytest.mark.skipif(torch.cuda.device_count() == 0, reason="No cuda device detected")
@pytest.mark.parametrize("device", get_available_devices()[1:])
def test_tensordict_error_messages(device):
    sub1 = TensorDict({"a": torch.randn(2, 3)}, [2])
    sub2 = TensorDict({"a": torch.randn(2, 3, device=device)}, [2])
    td1 = TensorDict({"sub": sub1}, [2])
    td2 = TensorDict({"sub": sub2}, [2])

    with pytest.raises(
        RuntimeError, match='tensors on different devices at key "sub" / "a"'
    ):
        torch.cat([td1, td2], 0)


def test_pad():
    dim0_left, dim0_right, dim1_left, dim1_right = [0, 1, 0, 2]
    td = TensorDict(
        {
            "a": torch.ones(3, 4, 1),
            "b": torch.zeros(3, 4, 1, 1),
        },
        batch_size=[3, 4],
    )

    padded_td = pad(td, [dim0_left, dim0_right, dim1_left, dim1_right], value=0.0)

    expected_a = torch.cat([torch.ones(3, 4, 1), torch.zeros(1, 4, 1)], dim=0)
    expected_a = torch.cat([expected_a, torch.zeros(4, 2, 1)], dim=1)

    assert padded_td["a"].shape == (4, 6, 1)
    assert padded_td["b"].shape == (4, 6, 1, 1)
    assert torch.equal(padded_td["a"], expected_a)
    padded_td._check_batch_size()


@pytest.mark.parametrize("device", get_available_devices())
def test_tensordict_indexing(device):
    torch.manual_seed(1)
    td = TensorDict({}, batch_size=(4, 5))
    td.set("key1", torch.randn(4, 5, 1, device=device))
    td.set("key2", torch.randn(4, 5, 6, device=device, dtype=torch.double))

    td_select = td[2, 2]
    td_select._check_batch_size()

    td_select = td[2, :2]
    td_select._check_batch_size()

    td_select = td[None, :2]
    td_select._check_batch_size()

    td_reconstruct = stack_td(list(td), 0, contiguous=False)
    assert (
        td_reconstruct == td
    ).all(), f"td and td_reconstruct differ, got {td} and {td_reconstruct}"

    superlist = [stack_td(list(_td), 0, contiguous=False) for _td in td]
    td_reconstruct = stack_td(superlist, 0, contiguous=False)
    assert (
        td_reconstruct == td
    ).all(), f"td and td_reconstruct differ, got {td == td_reconstruct}"

    x = torch.randn(4, 5, device=device)
    td = TensorDict(
        source={"key1": torch.zeros(3, 4, 5, device=device)},
        batch_size=[3, 4],
    )
    td[0].set_("key1", x)
    torch.testing.assert_close(td.get("key1")[0], x)
    torch.testing.assert_close(td.get("key1")[0], td[0].get("key1"))

    y = torch.randn(3, 5, device=device)
    td[:, 0].set_("key1", y)
    torch.testing.assert_close(td.get("key1")[:, 0], y)
    torch.testing.assert_close(td.get("key1")[:, 0], td[:, 0].get("key1"))


@pytest.mark.parametrize("device", get_available_devices())
def test_subtensordict_construction(device):
    torch.manual_seed(1)
    td = TensorDict({}, batch_size=(4, 5))
    val1 = torch.randn(4, 5, 1, device=device)
    val2 = torch.randn(4, 5, 6, dtype=torch.double, device=device)
    val1_copy = val1.clone()
    val2_copy = val2.clone()
    td.set("key1", val1)
    td.set("key2", val2)
    std1 = td.get_sub_tensordict(2)
    std2 = std1.get_sub_tensordict(2)
    idx = (2, 2)
    std_control = td.get_sub_tensordict(idx)
    assert (std_control.get("key1") == std2.get("key1")).all()
    assert (std_control.get("key2") == std2.get("key2")).all()

    # write values
    with pytest.raises(RuntimeError, match="is prohibited for existing tensors"):
        std_control.set("key1", torch.randn(1, device=device))
    with pytest.raises(RuntimeError, match="is prohibited for existing tensors"):
        std_control.set("key2", torch.randn(6, device=device, dtype=torch.double))

    subval1 = torch.randn(1, device=device)
    subval2 = torch.randn(6, device=device, dtype=torch.double)
    std_control.set_("key1", subval1)
    std_control.set_("key2", subval2)
    assert (val1_copy[idx] != subval1).all()
    assert (td.get("key1")[idx] == subval1).all()
    assert (td.get("key1")[1, 1] == val1_copy[1, 1]).all()

    assert (val2_copy[idx] != subval2).all()
    assert (td.get("key2")[idx] == subval2).all()
    assert (td.get("key2")[1, 1] == val2_copy[1, 1]).all()

    assert (std_control.get("key1") == std2.get("key1")).all()
    assert (std_control.get("key2") == std2.get("key2")).all()

    assert std_control.get_parent_tensordict() is td
    assert (
        std_control.get_parent_tensordict()
        is std2.get_parent_tensordict().get_parent_tensordict()
    )


@pytest.mark.parametrize("device", get_available_devices())
def test_mask_td(device):
    torch.manual_seed(1)
    d = {
        "key1": torch.randn(4, 5, 6, device=device),
        "key2": torch.randn(4, 5, 10, device=device),
    }
    mask = torch.zeros(4, 5, dtype=torch.bool, device=device).bernoulli_()
    td = TensorDict(batch_size=(4, 5), source=d)

    td_masked = torch.masked_select(td, mask)
    assert len(td_masked.get("key1")) == td_masked.shape[0]


@pytest.mark.parametrize("device", get_available_devices())
def test_unbind_td(device):
    torch.manual_seed(1)
    d = {
        "key1": torch.randn(4, 5, 6, device=device),
        "key2": torch.randn(4, 5, 10, device=device),
    }
    td = TensorDict(batch_size=(4, 5), source=d)
    td_unbind = torch.unbind(td, dim=1)
    assert (
        td_unbind[0].batch_size == td[:, 0].batch_size
    ), f"got {td_unbind[0].batch_size} and {td[:, 0].batch_size}"


@pytest.mark.parametrize("device", get_available_devices())
def test_cat_td(device):
    torch.manual_seed(1)
    d = {
        "key1": torch.randn(4, 5, 6, device=device),
        "key2": torch.randn(4, 5, 10, device=device),
        "key3": {"key4": torch.randn(4, 5, 10, device=device)},
    }
    td1 = TensorDict(batch_size=(4, 5), source=d, device=device)
    d = {
        "key1": torch.randn(4, 10, 6, device=device),
        "key2": torch.randn(4, 10, 10, device=device),
        "key3": {"key4": torch.randn(4, 10, 10, device=device)},
    }
    td2 = TensorDict(batch_size=(4, 10), source=d, device=device)

    td_cat = torch.cat([td1, td2], 1)
    assert td_cat.batch_size == torch.Size([4, 15])
    d = {
        "key1": torch.zeros(4, 15, 6, device=device),
        "key2": torch.zeros(4, 15, 10, device=device),
        "key3": {"key4": torch.zeros(4, 15, 10, device=device)},
    }
    td_out = TensorDict(batch_size=(4, 15), source=d, device=device)
    data_ptr_set_before = {val.data_ptr() for val in decompose(td_out)}
    torch.cat([td1, td2], 1, out=td_out)
    data_ptr_set_after = {val.data_ptr() for val in decompose(td_out)}
    assert data_ptr_set_before == data_ptr_set_after
    assert td_out.batch_size == torch.Size([4, 15])
    assert (td_out["key1"] != 0).all()
    assert (td_out["key2"] != 0).all()
    assert (td_out["key3", "key4"] != 0).all()


@pytest.mark.parametrize("device", get_available_devices())
def test_expand(device):
    torch.manual_seed(1)
    d = {
        "key1": torch.randn(4, 5, 6, device=device),
        "key2": torch.randn(4, 5, 10, device=device),
    }
    td1 = TensorDict(batch_size=(4, 5), source=d)
    td2 = td1.expand(3, 7, 4, 5)
    assert td2.batch_size == torch.Size([3, 7, 4, 5])
    assert td2.get("key1").shape == torch.Size([3, 7, 4, 5, 6])
    assert td2.get("key2").shape == torch.Size([3, 7, 4, 5, 10])


@pytest.mark.parametrize("device", get_available_devices())
def test_expand_with_singleton(device):
    torch.manual_seed(1)
    d = {
        "key1": torch.randn(1, 5, 6, device=device),
        "key2": torch.randn(1, 5, 10, device=device),
    }
    td1 = TensorDict(batch_size=(1, 5), source=d)
    td2 = td1.expand(3, 7, 4, 5)
    assert td2.batch_size == torch.Size([3, 7, 4, 5])
    assert td2.get("key1").shape == torch.Size([3, 7, 4, 5, 6])
    assert td2.get("key2").shape == torch.Size([3, 7, 4, 5, 10])


@pytest.mark.parametrize("device", get_available_devices())
def test_squeeze(device):
    torch.manual_seed(1)
    d = {
        "key1": torch.randn(4, 5, 6, device=device),
        "key2": torch.randn(4, 5, 10, device=device),
    }
    td1 = TensorDict(batch_size=(4, 5), source=d)
    td2 = torch.unsqueeze(td1, dim=1)
    assert td2.batch_size == torch.Size([4, 1, 5])

    td1b = torch.squeeze(td2, dim=1)
    assert td1b.batch_size == td1.batch_size


@pytest.mark.parametrize("device", get_available_devices())
def test_permute(device):
    torch.manual_seed(1)
    d = {
        "a": torch.randn(4, 5, 6, 9, device=device),
        "b": torch.randn(4, 5, 6, 7, device=device),
        "c": torch.randn(4, 5, 6, device=device),
    }
    td1 = TensorDict(batch_size=(4, 5, 6), source=d)
    td2 = torch.permute(td1, dims=(2, 1, 0))
    assert td2.shape == torch.Size((6, 5, 4))
    assert td2["a"].shape == torch.Size((6, 5, 4, 9))

    td2 = torch.permute(td1, dims=(-1, -3, -2))
    assert td2.shape == torch.Size((6, 4, 5))
    assert td2["c"].shape == torch.Size((6, 4, 5))

    td2 = torch.permute(td1, dims=(0, 1, 2))
    assert td2["a"].shape == torch.Size((4, 5, 6, 9))

    t = TensorDict({"a": torch.randn(3, 4, 1)}, [3, 4])
    torch.permute(t, dims=(1, 0)).set("b", torch.randn(4, 3))
    assert t["b"].shape == torch.Size((3, 4))

    torch.permute(t, dims=(1, 0)).fill_("a", 0.0)
    assert torch.sum(t["a"]) == torch.Tensor([0])


@pytest.mark.parametrize("device", get_available_devices())
def test_permute_applied_twice(device):
    torch.manual_seed(1)
    d = {
        "a": torch.randn(4, 5, 6, 9, device=device),
        "b": torch.randn(4, 5, 6, 7, device=device),
        "c": torch.randn(4, 5, 6, device=device),
    }
    td1 = TensorDict(batch_size=(4, 5, 6), source=d)
    td2 = torch.permute(td1, dims=(2, 1, 0))
    td3 = torch.permute(td2, dims=(2, 1, 0))
    assert td3 is td1
    td1 = TensorDict(batch_size=(4, 5, 6), source=d)
    td2 = torch.permute(td1, dims=(2, 1, 0))
    td3 = torch.permute(td2, dims=(0, 1, 2))
    assert td3 is not td1


@pytest.mark.parametrize("device", get_available_devices())
def test_permute_exceptions(device):
    torch.manual_seed(1)
    d = {
        "a": torch.randn(4, 5, 6, 7, device=device),
        "b": torch.randn(4, 5, 6, 8, 9, device=device),
    }
    td1 = TensorDict(batch_size=(4, 5, 6), source=d)

    with pytest.raises(RuntimeError):
        td2 = td1.permute(1, 1, 0)
        _ = td2.shape

    with pytest.raises(RuntimeError):
        td2 = td1.permute(3, 2, 1, 0)
        _ = td2.shape

    with pytest.raises(RuntimeError):
        td2 = td1.permute(2, -1, 0)
        _ = td2.shape

    with pytest.raises(IndexError):
        td2 = td1.permute(2, 3, 0)
        _ = td2.shape

    with pytest.raises(IndexError):
        td2 = td1.permute(2, -4, 0)
        _ = td2.shape

    with pytest.raises(RuntimeError):
        td2 = td1.permute(2, 1)
        _ = td2.shape


@pytest.mark.parametrize("device", get_available_devices())
def test_permute_with_tensordict_operations(device):
    torch.manual_seed(1)
    d = {
        "a": torch.randn(20, 6, 9, device=device),
        "b": torch.randn(20, 6, 7, device=device),
        "c": torch.randn(20, 6, device=device),
    }
    td1 = TensorDict(batch_size=(20, 6), source=d).view(4, 5, 6).permute(2, 1, 0)
    assert td1.shape == torch.Size((6, 5, 4))

    d = {
        "a": torch.randn(4, 5, 6, 7, 9, device=device),
        "b": torch.randn(4, 5, 6, 7, 7, device=device),
        "c": torch.randn(4, 5, 6, 7, device=device),
    }
    td1 = TensorDict(batch_size=(4, 5, 6, 7), source=d)[
        :, :, :, torch.tensor([1, 2])
    ].permute(3, 2, 1, 0)
    assert td1.shape == torch.Size((2, 6, 5, 4))

    d = {
        "a": torch.randn(4, 5, 9, device=device),
        "b": torch.randn(4, 5, 7, device=device),
        "c": torch.randn(4, 5, device=device),
    }
    td1 = stack_td(
        [TensorDict(batch_size=(4, 5), source=d).clone() for _ in range(6)],
        2,
        contiguous=False,
    ).permute(2, 1, 0)
    assert td1.shape == torch.Size((6, 5, 4))


def test_inferred_view_size():
    td = TensorDict({"a": torch.randn(3, 4)}, [3, 4])
    assert td.view(-1).view(-1, 4) is td

    assert td.view(-1, 4) is td
    assert td.view(3, -1) is td
    assert td.view(3, 4) is td
    assert td.view(-1, 12).shape == torch.Size([1, 12])


@pytest.mark.parametrize(
    "ellipsis_index, expected_index",
    [
        (..., (slice(None), slice(None), slice(None), slice(None), slice(None))),
        ((0, ..., 0), (0, slice(None), slice(None), slice(None), 0)),
        ((..., 0), (slice(None), slice(None), slice(None), slice(None), 0)),
        ((0, ...), (0, slice(None), slice(None), slice(None), slice(None))),
        (
            (slice(1, 2), ...),
            (slice(1, 2), slice(None), slice(None), slice(None), slice(None)),
        ),
    ],
)
def test_convert_ellipsis_to_idx_valid(ellipsis_index, expected_index):
    torch.manual_seed(1)
    batch_size = [3, 4, 5, 6, 7]

    assert convert_ellipsis_to_idx(ellipsis_index, batch_size) == expected_index


@pytest.mark.parametrize(
    "ellipsis_index, expectation",
    [
        ((..., 0, ...), pytest.raises(RuntimeError)),
        ((0, ..., 0, ...), pytest.raises(RuntimeError)),
    ],
)
def test_convert_ellipsis_to_idx_invalid(ellipsis_index, expectation):
    torch.manual_seed(1)
    batch_size = [3, 4, 5, 6, 7]

    with expectation:
        _ = convert_ellipsis_to_idx(ellipsis_index, batch_size)


TD_BATCH_SIZE = 4


@pytest.mark.parametrize(
    "td_name",
    [
        "td",
        "stacked_td",
        "sub_td",
        "sub_td2",
        "idx_td",
        "memmap_td",
        "unsqueezed_td",
        "squeezed_td",
        "td_reset_bs",
        "nested_td",
        "nested_tensorclass",
        "permute_td",
        "nested_stacked_td",
        "td_params",
        pytest.param(
            "td_h5", marks=pytest.mark.skipif(not _has_h5py, reason="h5py not found.")
        ),
    ],
)
@pytest.mark.parametrize("device", get_available_devices())
class TestTensorDicts(TestTensorDictsBase):
    def test_permute_applied_twice(self, td_name, device):
        torch.manual_seed(0)
        tensordict = getattr(self, td_name)(device)
        for _ in range(10):
            p = torch.randperm(4)
            inv_p = p.argsort()
            other_p = inv_p
            while (other_p == inv_p).all():
                other_p = torch.randperm(4)
            other_p = tuple(other_p.tolist())
            p = tuple(p.tolist())
            inv_p = tuple(inv_p.tolist())
            if td_name in ("td_params",):
                assert (
                    tensordict.permute(*p).permute(*inv_p)._param_td
                    is tensordict._param_td
                )
                assert (
                    tensordict.permute(*p).permute(*other_p)._param_td
                    is not tensordict._param_td
                )
                assert (
                    torch.permute(tensordict, p).permute(inv_p)._param_td
                    is tensordict._param_td
                )
                assert (
                    torch.permute(tensordict, p).permute(other_p)._param_td
                    is not tensordict._param_td
                )
            else:
                assert tensordict.permute(*p).permute(*inv_p) is tensordict
                assert tensordict.permute(*p).permute(*other_p) is not tensordict
                assert torch.permute(tensordict, p).permute(inv_p) is tensordict
                assert torch.permute(tensordict, p).permute(other_p) is not tensordict

    def test_to_tensordict(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td2 = td.to_tensordict()
        assert (td2 == td).all()

    @pytest.mark.parametrize("strict", [True, False])
    @pytest.mark.parametrize("inplace", [True, False])
    def test_select(self, td_name, device, strict, inplace):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        keys = ["a"]
        if td_name == "td_h5":
            with pytest.raises(NotImplementedError, match="Cannot call select"):
                td.select(*keys, strict=strict, inplace=inplace)
            return

        if td_name in ("nested_stacked_td", "nested_td"):
            keys += [("my_nested_td", "inner")]

        td2 = td.select(*keys, strict=strict, inplace=inplace)
        if inplace:
            assert td2 is td
        else:
            assert td2 is not td
        if td_name == "saved_td":
            assert (len(list(td2.keys())) == len(keys)) and ("a" in td2.keys())
            assert (len(list(td2.clone().keys())) == len(keys)) and (
                "a" in td2.clone().keys()
            )
        else:
            assert (len(list(td2.keys(True, True))) == len(keys)) and (
                "a" in td2.keys()
            )
            assert (len(list(td2.clone().keys(True, True))) == len(keys)) and (
                "a" in td2.clone().keys()
            )

    @pytest.mark.parametrize("strict", [True, False])
    def test_select_exception(self, td_name, device, strict):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_h5":
            with pytest.raises(NotImplementedError, match="Cannot call select"):
                _ = td.select("tada", strict=strict)
            return

        if strict:
            with pytest.raises(KeyError):
                _ = td.select("tada", strict=strict)
        else:
            td2 = td.select("tada", strict=strict)
            assert td2 is not td
            assert len(list(td2.keys())) == 0

    def test_exclude(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_h5":
            with pytest.raises(NotImplementedError, match="Cannot call exclude"):
                _ = td.exclude("a")
            return
        td2 = td.exclude("a")
        assert td2 is not td
        assert (
            len(list(td2.keys())) == len(list(td.keys())) - 1 and "a" not in td2.keys()
        )
        assert (
            len(list(td2.clone().keys())) == len(list(td.keys())) - 1
            and "a" not in td2.clone().keys()
        )

        with td.unlock_():
            td2 = td.exclude("a", inplace=True)
        assert td2 is td

    def test_assert(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        with pytest.raises(
            ValueError,
            match="Converting a tensordict to boolean value is not permitted",
        ):
            assert td

    def test_expand(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        batch_size = td.batch_size
        expected_size = torch.Size([3, *batch_size])

        new_td = td.expand(3, *batch_size)
        assert new_td.batch_size == expected_size
        assert all((_new_td == td).all() for _new_td in new_td)

        new_td_torch_size = td.expand(expected_size)
        assert new_td_torch_size.batch_size == expected_size
        assert all((_new_td == td).all() for _new_td in new_td_torch_size)

        new_td_iterable = td.expand([3, *batch_size])
        assert new_td_iterable.batch_size == expected_size
        assert all((_new_td == td).all() for _new_td in new_td_iterable)

    def test_cast_to(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td_device = td.to("cpu:1")
        assert td_device.device == torch.device("cpu:1")
        td_dtype = td.to(torch.int)
        assert all(t.dtype == torch.int for t in td_dtype.values(True, True))
        del td_dtype
        # device (str), dtype
        td_dtype_device = td.to("cpu:1", torch.int)
        assert all(t.dtype == torch.int for t in td_dtype_device.values(True, True))
        assert td_dtype_device.device == torch.device("cpu:1")
        del td_dtype_device
        # device, dtype
        td_dtype_device = td.to(torch.device("cpu:1"), torch.int)
        assert all(t.dtype == torch.int for t in td_dtype_device.values(True, True))
        assert td_dtype_device.device == torch.device("cpu:1")
        del td_dtype_device
        # example tensor
        td_dtype_device = td.to(torch.randn(3, dtype=torch.half, device="cpu:1"))
        assert all(t.dtype == torch.half for t in td_dtype_device.values(True, True))
        # tensor on cpu:1 is actually on cpu. This is still meaningful for tensordicts on cuda.
        assert td_dtype_device.device == torch.device("cpu")
        del td_dtype_device
        # example td
        td_dtype_device = td.to(
            other=TensorDict(
                {"a": torch.randn(3, dtype=torch.half, device="cpu:1")},
                [],
                device="cpu:1",
            )
        )
        assert all(t.dtype == torch.half for t in td_dtype_device.values(True, True))
        assert td_dtype_device.device == torch.device("cpu:1")
        del td_dtype_device
        # example td, many dtypes
        td_nodtype_device = td.to(
            other=TensorDict(
                {
                    "a": torch.randn(3, dtype=torch.half, device="cpu:1"),
                    "b": torch.randint(10, ()),
                },
                [],
                device="cpu:1",
            )
        )
        assert all(t.dtype != torch.half for t in td_nodtype_device.values(True, True))
        assert td_nodtype_device.device == torch.device("cpu:1")
        del td_nodtype_device
        # batch-size: check errors (or not)
        if td_name in (
            "stacked_td",
            "unsqueezed_td",
            "squeezed_td",
            "permute_td",
            "nested_stacked_td",
        ):
            with pytest.raises(TypeError, match="Cannot pass batch-size to a "):
                td_dtype_device = td.to(
                    torch.device("cpu:1"), torch.int, batch_size=torch.Size([])
                )
        else:
            td_dtype_device = td.to(
                torch.device("cpu:1"), torch.int, batch_size=torch.Size([])
            )
            assert all(t.dtype == torch.int for t in td_dtype_device.values(True, True))
            assert td_dtype_device.device == torch.device("cpu:1")
            assert td_dtype_device.batch_size == torch.Size([])
            del td_dtype_device
        if td_name in (
            "stacked_td",
            "unsqueezed_td",
            "squeezed_td",
            "permute_td",
            "nested_stacked_td",
        ):
            with pytest.raises(TypeError, match="Cannot pass batch-size to a "):
                td.to(batch_size=torch.Size([]))
        else:
            td_batchsize = td.to(batch_size=torch.Size([]))
            assert td_batchsize.batch_size == torch.Size([])
            del td_batchsize

    # Deprecated:
    # def test_cast(self, td_name, device):
    #     torch.manual_seed(1)
    #     td = getattr(self, td_name)(device)
    #     td_td = td.to(TensorDict)
    #     assert (td == td_td).all()

    def test_broadcast(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        sub_td = td[:, :2].to_tensordict()
        sub_td.zero_()
        sub_dict = sub_td.to_dict()
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:, :2] = sub_dict
        assert (td[:, :2] == 0).all()

    @pytest.mark.parametrize("call_del", [True, False])
    def test_remove(self, td_name, device, call_del):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        with td.unlock_():
            if call_del:
                del td["a"]
            else:
                td = td.del_("a")
        assert td is not None
        assert "a" not in td.keys()
        if td_name in ("sub_td", "sub_td2"):
            return
        td.lock_()
        with pytest.raises(RuntimeError, match="locked"):
            del td["b"]

    def test_set_unexisting(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td.is_locked:
            with pytest.raises(
                RuntimeError,
                match="Cannot modify locked TensorDict. For in-place modification",
            ):
                td.set("z", torch.ones_like(td.get("a")))
        else:
            td.set("z", torch.ones_like(td.get("a")))
            assert (td.get("z") == 1).all()

    def test_fill_(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        new_td = td_set.fill_("a", 0.1)
        assert (td.get("a") == 0.1).all()
        assert new_td is td_set

    def test_shape(self, td_name, device):
        td = getattr(self, td_name)(device)
        assert td.shape == td.batch_size

    def test_flatten_unflatten(self, td_name, device):
        td = getattr(self, td_name)(device)
        shape = td.shape[:3]
        td_flat = td.flatten(0, 2)
        td_unflat = td_flat.unflatten(0, shape)
        assert (td.to_tensordict() == td_unflat).all()
        assert td.batch_size == td_unflat.batch_size

    def test_flatten_unflatten_bis(self, td_name, device):
        td = getattr(self, td_name)(device)
        shape = td.shape[1:4]
        td_flat = td.flatten(1, 3)
        td_unflat = td_flat.unflatten(1, shape)
        assert (td.to_tensordict() == td_unflat).all()
        assert td.batch_size == td_unflat.batch_size

    def test_masked_fill_(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        mask = torch.zeros(td.shape, dtype=torch.bool, device=device).bernoulli_()
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        new_td = td_set.masked_fill_(mask, -10.0)
        assert new_td is td_set
        for item in td.values():
            assert (item[mask] == -10).all(), item[mask]

    def test_set_nested_batch_size(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()
        batch_size = torch.Size([*td.batch_size, 3])
        td.set("some_other_td", TensorDict({}, batch_size))
        assert td["some_other_td"].batch_size == batch_size

    def test_lock(self, td_name, device):
        td = getattr(self, td_name)(device)
        is_locked = td.is_locked
        for item in td.values():
            if isinstance(item, TensorDictBase):
                assert item.is_locked == is_locked
        if isinstance(td, SubTensorDict):
            with pytest.raises(RuntimeError, match="the parent tensordict instead"):
                td.is_locked = not is_locked
            return
        td.is_locked = not is_locked
        assert td.is_locked != is_locked
        for _, item in td.items():
            if isinstance(item, TensorDictBase):
                assert item.is_locked != is_locked
        td.lock_()
        assert td.is_locked
        for _, item in td.items():
            if isinstance(item, TensorDictBase):
                assert item.is_locked
        td.unlock_()
        assert not td.is_locked
        for _, item in td.items():
            if isinstance(item, TensorDictBase):
                assert not item.is_locked

    def test_lock_write(self, td_name, device):
        td = getattr(self, td_name)(device)
        if isinstance(td, SubTensorDict):
            with pytest.raises(RuntimeError, match="the parent tensordict instead"):
                td.lock_()
            return

        td.lock_()
        td_clone = td.clone()
        assert not td_clone.is_locked
        td_clone = td.to_tensordict()
        assert not td_clone.is_locked
        assert td.is_locked
        if td_name == "td_h5":
            td.unlock_()
            for key in list(td.keys()):
                del td[key]
            td.lock_()
        else:
            td = td.select(inplace=True)
        for key, item in td_clone.items(True):
            with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
                td.set(key, item)
        td.unlock_()
        for key, item in td_clone.items(True):
            td.set(key, item)
        td.lock_()
        for key, item in td_clone.items(True):
            with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
                td.set(key, item)
            if td_name == "td_params":
                td_set = td.data
            else:
                td_set = td
            td_set.set_(key, item)

    def test_unlock(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()
        assert not td.is_locked
        if td.device is not None:
            assert td.device.type == "cuda" or not td.is_shared()
        else:
            assert not td.is_shared()
        assert not td.is_memmap()

    def test_lock_nested(self, td_name, device):
        td = getattr(self, td_name)(device)
        if td_name in ("sub_td", "sub_td2") and td.is_locked:
            with pytest.raises(RuntimeError, match="Cannot unlock"):
                td.unlock_()
        else:
            td.unlock_()
        td.set(("some", "nested"), torch.zeros(td.shape))
        if td_name in ("sub_td", "sub_td2") and not td.is_locked:
            with pytest.raises(RuntimeError, match="Cannot lock"):
                td.lock_()
            return
        td.lock_()
        some = td.get("some")
        assert some.is_locked
        with pytest.raises(RuntimeError):
            some.unlock_()
        del td
        some.unlock_()

    # @pytest.mark.parametrize("op", ["keys_root", "keys_nested", "values", "items"])
    @pytest.mark.parametrize("op", ["flatten", "unflatten"])
    def test_cache(self, td_name, device, op):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        try:
            td.lock_()
        except Exception:
            return
        if op == "keys_root":
            a = list(td.keys())
            b = list(td.keys())
            assert a == b
        elif op == "keys_nested":
            a = list(td.keys(True))
            b = list(td.keys(True))
            assert a == b
        elif op == "values":
            a = list(td.values(True))
            b = list(td.values(True))
            assert all((_a == _b).all() for _a, _b in zip(a, b))
        elif op == "items":
            keys_a, values_a = zip(*td.items(True))
            keys_b, values_b = zip(*td.items(True))
            assert all((_a == _b).all() for _a, _b in zip(values_a, values_b))
            assert keys_a == keys_b
        elif op == "flatten":
            a = td.flatten_keys()
            b = td.flatten_keys()
            assert a is b
        elif op == "unflatten":
            a = td.unflatten_keys()
            b = td.unflatten_keys()
            assert a is b

        if td_name != "td_params":
            assert len(td._cache)
        td.unlock_()
        assert td._cache is None
        for val in td.values(True):
            if is_tensor_collection(val):
                assert td._cache is None

    def test_enter_exit(self, td_name, device):
        torch.manual_seed(1)
        if td_name in ("sub_td", "sub_td2"):
            return
        td = getattr(self, td_name)(device)
        is_locked = td.is_locked
        with td.lock_() as other:
            assert other is td
            assert td.is_locked
            with td.unlock_() as other:
                assert other is td
                assert not td.is_locked
            assert td.is_locked
        assert td.is_locked is is_locked

    def test_lock_change_names(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        try:
            td.names = [str(i) for i in range(td.ndim)]
            td.lock_()
        except Exception:
            return
        # cache values
        list(td.values(True))
        td.names = [str(-i) for i in range(td.ndim)]
        for val in td.values(True):
            if not is_tensor_collection(val):
                continue
            assert val.names[: td.ndim] == [str(-i) for i in range(td.ndim)]

    def test_sorted_keys(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        sorted_keys = td.sorted_keys
        i = -1
        for i, (key1, key2) in enumerate(zip(sorted_keys, td.keys())):  # noqa: B007
            assert key1 == key2
        assert i == len(td.keys()) - 1
        if td.is_locked:
            assert td._cache.get("sorted_keys", None) is not None
            td.unlock_()
            assert td._cache is None
        elif td_name not in ("sub_td", "sub_td2"):  # we cannot lock sub tensordicts
            if isinstance(td, _CustomOpTensorDict):
                target = td._source
            else:
                target = td
            assert target._cache is None
            td.lock_()
            _ = td.sorted_keys
            assert target._cache.get("sorted_keys", None) is not None
            td.unlock_()
            assert target._cache is None

    def test_masked_fill(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        mask = torch.zeros(td.shape, dtype=torch.bool, device=device).bernoulli_()
        new_td = td.masked_fill(mask, -10.0)
        assert new_td is not td
        for item in new_td.values():
            assert (item[mask] == -10).all()

    def test_zero_(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            with pytest.raises(
                RuntimeError, match="a leaf Variable that requires grad"
            ):
                new_td = td.zero_()
            return
        new_td = td.zero_()
        assert new_td is td
        for k in td.keys():
            assert (td.get(k) == 0).all()

    @pytest.mark.parametrize("inplace", [False, True])
    def test_apply(self, td_name, device, inplace):
        td = getattr(self, td_name)(device)
        td_c = td.to_tensordict()
        if inplace and td_name == "td_params":
            with pytest.raises(ValueError, match="Failed to update"):
                td.apply(lambda x: x + 1, inplace=inplace)
            return
        td_1 = td.apply(lambda x: x + 1, inplace=inplace)
        if inplace:
            for key in td.keys(True, True):
                assert (td_c[key] + 1 == td[key]).all()
                assert (td_1[key] == td[key]).all()
        else:
            for key in td.keys(True, True):
                assert (td_c[key] + 1 != td[key]).any()
                assert (td_1[key] == td[key] + 1).all()

    @pytest.mark.parametrize("inplace", [False, True])
    def test_apply_other(self, td_name, device, inplace):
        td = getattr(self, td_name)(device)
        td_c = td.to_tensordict()
        if inplace and td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_1 = td_set.apply(lambda x, y: x + y, td_c, inplace=inplace)
        if inplace:
            for key in td.keys(True, True):
                assert (td_c[key] * 2 == td[key]).all()
                assert (td_1[key] == td[key]).all()
        else:
            for key in td.keys(True, True):
                assert (td_c[key] * 2 != td[key]).any()
                assert (td_1[key] == td[key] * 2).all()

    def test_from_empty(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        new_td = TensorDict({}, batch_size=td.batch_size, device=device)
        for key, item in td.items():
            new_td.set(key, item)
        assert_allclose_td(td, new_td)
        assert td.device == new_td.device
        assert td.shape == new_td.shape

    def test_masking(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        while True:
            mask = torch.zeros(
                td.batch_size, dtype=torch.bool, device=device
            ).bernoulli_(0.8)
            if not mask.all() and mask.any():
                break
        td_masked = td[mask]
        td_masked2 = torch.masked_select(td, mask)
        assert_allclose_td(td_masked, td_masked2)
        assert td_masked.batch_size[0] == mask.sum()
        assert td_masked.batch_dims == 1

        # mask_list = mask.cpu().numpy().tolist()
        # td_masked3 = td[mask_list]
        # assert_allclose_td(td_masked3, td_masked2)
        # assert td_masked3.batch_size[0] == mask.sum()
        # assert td_masked3.batch_dims == 1

    def test_entry_type(self, td_name, device):
        td = getattr(self, td_name)(device)
        for key in td.keys(include_nested=True):
            assert type(td.get(key)) is td.entry_class(key)

    def test_equal(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert (td == td.to_tensordict()).all()
        td0 = td.to_tensordict().zero_()
        assert (td != td0).any()

    def test_equal_float(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set.zero_()
        assert (td == 0.0).all()
        td0 = td.clone()
        if td_name == "td_params":
            td_set = td0.data
        else:
            td_set = td0
        td_set.zero_()
        assert (td0 != 1.0).all()

    def test_equal_other(self, td_name, device):
        td = getattr(self, td_name)(device)
        assert not td == "z"
        assert td != "z"

    def test_equal_int(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set.zero_()
        assert (td == 0).all()
        td0 = td.to_tensordict().zero_()
        assert (td0 != 1).all()

    def test_equal_tensor(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set.zero_()
        assert (td == torch.zeros([], dtype=torch.int, device=device)).all()
        td0 = td.to_tensordict().zero_()
        assert (td0 != torch.ones([], dtype=torch.int, device=device)).all()

    def test_equal_dict(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert (td == td.to_dict()).all()
        td0 = td.to_tensordict().zero_().to_dict()
        assert (td != td0).any()

    @pytest.mark.parametrize("dim", [0, 1, 2, 3, -1, -2, -3])
    def test_gather(self, td_name, device, dim):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        index = torch.ones(td.shape, device=td.device, dtype=torch.long)
        other_dim = dim + index.ndim if dim < 0 else dim
        idx = (*[slice(None) for _ in range(other_dim)], slice(2))
        index = index[idx]
        index = index.cumsum(dim=other_dim) - 1
        # gather
        td_gather = torch.gather(td, dim=dim, index=index)
        # gather with out
        td_gather.zero_()
        out = td_gather.clone()
        if td_name == "td_params":
            with pytest.raises(
                RuntimeError, match="don't support automatic differentiation"
            ):
                torch.gather(td, dim=dim, index=index, out=out)
            return
        td_gather2 = torch.gather(td, dim=dim, index=index, out=out)
        assert (td_gather2 != 0).any()

    def test_where(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        mask = torch.zeros(td.shape, dtype=torch.bool, device=device).bernoulli_()
        td_where = torch.where(mask, td, 0)
        for k in td.keys(True, True):
            assert (td_where.get(k)[~mask] == 0).all()
        td_where = torch.where(mask, td, torch.ones_like(td))
        for k in td.keys(True, True):
            assert (td_where.get(k)[~mask] == 1).all()
        td_where = td.clone()

        if td_name == "td_h5":
            with pytest.raises(
                RuntimeError,
                match="Cannot use a persistent tensordict as output of torch.where",
            ):
                torch.where(mask, td, torch.ones_like(td), out=td_where)
            return
        torch.where(mask, td, torch.ones_like(td), out=td_where)
        for k in td.keys(True, True):
            assert (td_where.get(k)[~mask] == 1).all()

    def test_where_pad(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        # test with other empty td
        mask = torch.zeros(td.shape, dtype=torch.bool, device=td.device).bernoulli_()
        if td_name in ("td_h5",):
            td_full = td.to_tensordict()
        else:
            td_full = td
        td_empty = td_full.empty()
        result = td.where(mask, td_empty, pad=1)
        for v in result.values(True, True):
            assert (v[~mask] == 1).all()
        td_empty = td_full.empty()
        result = td_empty.where(~mask, td, pad=1)
        for v in result.values(True, True):
            assert (v[~mask] == 1).all()
        # with output
        td_out = td_full.empty()
        result = td.where(mask, td_empty, pad=1, out=td_out)
        for v in result.values(True, True):
            assert (v[~mask] == 1).all()
        if td_name not in ("td_params",):
            assert result is td_out
        else:
            assert isinstance(result, TensorDictParams)
        td_out = td_full.empty()
        td_empty = td_full.empty()
        result = td_empty.where(~mask, td, pad=1, out=td_out)
        for v in result.values(True, True):
            assert (v[~mask] == 1).all()
        assert result is td_out

        with pytest.raises(KeyError, match="not found and no pad value provided"):
            td.where(mask, td_full.empty())
        with pytest.raises(KeyError, match="not found and no pad value provided"):
            td_full.empty().where(mask, td)

    def test_masking_set(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        mask = torch.zeros(td.batch_size, dtype=torch.bool, device=device).bernoulli_(
            0.8
        )
        n = mask.sum()
        d = td.ndimension()
        pseudo_td = td.apply(
            lambda item: torch.zeros(
                (n, *item.shape[d:]), dtype=item.dtype, device=device
            ),
            batch_size=[n, *td.batch_size[d:]],
        )

        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td

        td_set[mask] = pseudo_td
        for item in td.values():
            assert (item[mask] == 0).all()

    @pytest.mark.skipif(
        torch.cuda.device_count() == 0, reason="No cuda device detected"
    )
    @pytest.mark.parametrize("device_cast", [0, "cuda:0", torch.device("cuda:0")])
    def test_pin_memory(self, td_name, device_cast, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()
        if device.type == "cuda":
            with pytest.raises(RuntimeError, match="cannot pin"):
                td.pin_memory()
            return
        td.pin_memory()
        td_device = td.to(device_cast)
        _device_cast = torch.device(device_cast)
        assert td_device.device == _device_cast
        assert td_device.clone().device == _device_cast
        if device != _device_cast:
            assert td_device is not td
        for item in td_device.values():
            assert item.device == _device_cast
        for item in td_device.clone().values():
            assert item.device == _device_cast
        # assert type(td_device) is type(td)
        assert_allclose_td(td, td_device.to(device))

    def test_indexed_properties(self, td_name, device):
        td = getattr(self, td_name)(device)
        td_index = td[0]
        assert td_index.is_memmap() is td.is_memmap()
        assert td_index.is_shared() is td.is_shared()
        assert td_index.device == td.device

    @pytest.mark.parametrize(
        "idx",
        [
            (..., None),
            (None, ...),
            (None,),
            None,
            (slice(None), None),
            (0, None),
            (None, slice(None), slice(None)),
            (None, ..., None),
            (None, 1, ..., None),
            (1, ..., None),
            (..., None, 0),
            ([1], ..., None),
        ],
    )
    def test_index_none(self, td_name, device, idx):
        td = getattr(self, td_name)(device)
        tdnone = td[idx]
        tensor = torch.zeros(td.shape)
        assert tdnone.shape == tensor[idx].shape, idx
        # Fixed by 451
        # if td_name == "td_h5":
        #     with pytest.raises(TypeError, match="can't process None"):
        #         assert (tdnone.to_tensordict() == td.to_tensordict()[idx]).all()
        #     return
        assert (tdnone.to_tensordict() == td.to_tensordict()[idx]).all()

    @pytest.mark.skipif(
        torch.cuda.device_count() == 0, reason="No cuda device detected"
    )
    @pytest.mark.parametrize("device_cast", get_available_devices())
    def test_cast_device(self, td_name, device, device_cast):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td_device = td.to(device_cast)

        for item in td_device.values():
            assert item.device == device_cast
        for item in td_device.clone().values():
            assert item.device == device_cast

        assert td_device.device == device_cast, (
            f"td_device first tensor device is " f"{next(td_device.items())[1].device}"
        )
        assert td_device.clone().device == device_cast
        if device_cast != td.device:
            assert td_device is not td
        assert td_device.to(device_cast) is td_device
        assert td.to(device) is td
        assert_allclose_td(td, td_device.to(device))

    @pytest.mark.skipif(
        torch.cuda.device_count() == 0, reason="No cuda device detected"
    )
    def test_cpu_cuda(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td_device = td.cuda()
        td_back = td_device.cpu()
        assert td_device.device == torch.device("cuda")
        assert td_back.device == torch.device("cpu")

    def test_state_dict(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        sd = td.state_dict()
        td_zero = td.clone().detach().zero_()
        td_zero.load_state_dict(sd)
        assert_allclose_td(td, td_zero)

    def test_state_dict_strict(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        sd = td.state_dict()
        td_zero = td.clone().detach().zero_()
        del sd["a"]
        td_zero.load_state_dict(sd, strict=False)
        with pytest.raises(RuntimeError):
            td_zero.load_state_dict(sd, strict=True)

    def test_state_dict_assign(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        sd = td.state_dict()
        td_zero = td.clone().detach().zero_()
        shallow_copy = td_zero.clone(False)
        td_zero.load_state_dict(sd, assign=True)
        assert (shallow_copy == 0).all()
        assert_allclose_td(td, td_zero)

    @pytest.mark.parametrize("dim", range(4))
    def test_unbind(self, td_name, device, dim):
        if td_name not in ["sub_td", "idx_td", "td_reset_bs"]:
            torch.manual_seed(1)
            td = getattr(self, td_name)(device)
            td_unbind = torch.unbind(td, dim=dim)
            assert (td == stack_td(td_unbind, dim).contiguous()).all()
            idx = (slice(None),) * dim + (0,)
            assert (td[idx] == td_unbind[0]).all()

    @pytest.mark.parametrize("squeeze_dim", [0, 1])
    def test_unsqueeze(self, td_name, device, squeeze_dim):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()  # make sure that the td is not locked
        td_unsqueeze = torch.unsqueeze(td, dim=squeeze_dim)
        tensor = torch.ones_like(td.get("a").unsqueeze(squeeze_dim))
        if td_name in ("sub_td", "sub_td2"):
            td_unsqueeze.set_("a", tensor)
        else:
            td_unsqueeze.set("a", tensor)
        assert (td_unsqueeze.get("a") == tensor).all()
        assert (td.get("a") == tensor.squeeze(squeeze_dim)).all()
        # the tensors should match
        assert _compare_tensors_identity(td_unsqueeze.squeeze(squeeze_dim), td)
        assert (td_unsqueeze.get("a") == 1).all()
        assert (td.get("a") == 1).all()

    def test_squeeze(self, td_name, device, squeeze_dim=-1):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()  # make sure that the td is not locked
        td_squeeze = torch.squeeze(td, dim=-1)
        tensor_squeeze_dim = td.batch_dims + squeeze_dim
        tensor = torch.ones_like(td.get("a").squeeze(tensor_squeeze_dim))
        if td_name in ("sub_td", "sub_td2"):
            td_squeeze.set_("a", tensor)
        else:
            td_squeeze.set("a", tensor)
        assert td.batch_size[squeeze_dim] == 1
        assert (td_squeeze.get("a") == tensor).all()
        assert (td.get("a") == tensor.unsqueeze(tensor_squeeze_dim)).all()
        if td_name != "unsqueezed_td":
            assert _compare_tensors_identity(td_squeeze.unsqueeze(squeeze_dim), td)
        else:
            assert td_squeeze is td._source
        assert (td_squeeze.get("a") == 1).all()
        assert (td.get("a") == 1).all()

    def test_squeeze_with_none(self, td_name, device, squeeze_dim=None):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td_squeeze = torch.squeeze(td, dim=None)
        tensor = torch.ones_like(td.get("a").squeeze())
        if td_name == "td_params":
            with pytest.raises(ValueError, match="Failed to update"):
                td_squeeze.set_("a", tensor)
            return
        td_squeeze.set_("a", tensor)
        assert (td_squeeze.get("a") == tensor).all()
        if td_name == "unsqueezed_td":
            assert td_squeeze._source is td
        assert (td_squeeze.get("a") == 1).all()
        assert (td.get("a") == 1).all()

    @pytest.mark.parametrize("nested", [True, False])
    def test_exclude_missing(self, td_name, device, nested):
        if td_name == "td_h5":
            raise pytest.skip("exclude not implemented for PersitentTensorDict")
        td = getattr(self, td_name)(device)
        if nested:
            td2 = td.exclude("this key is missing", ("this one too",))
        else:
            td2 = td.exclude(
                "this key is missing",
            )
        assert (td == td2).all()

    @pytest.mark.parametrize("nested", [True, False])
    def test_exclude_nested(self, td_name, device, nested):
        if td_name == "td_h5":
            raise pytest.skip("exclude not implemented for PersitentTensorDict")
        td = getattr(self, td_name)(device)
        td.unlock_()  # make sure that the td is not locked
        if td_name == "stacked_td":
            for _td in td.tensordicts:
                _td["newnested", "first"] = torch.randn(_td.shape)
        else:
            td["newnested", "first"] = torch.randn(td.shape)
        if nested:
            td2 = td.exclude("a", ("newnested", "first"))
            assert "a" in td.keys(), list(td.keys())
            assert "a" not in td2.keys()
            assert ("newnested", "first") in td.keys(True), list(td.keys(True))
            assert ("newnested", "first") not in td2.keys(True)
        else:
            td2 = td.exclude(
                "a",
            )
            assert "a" in td.keys()
            assert "a" not in td2.keys()
        if td_name not in (
            "sub_td",
            "sub_td2",
            "unsqueezed_td",
            "squeezed_td",
            "permute_td",
            "td_params",
        ):
            # TODO: document this as an edge-case: with a sub-tensordict, exclude acts on the parent tensordict
            # perhaps exclude should return an error in these cases?
            assert type(td2) is type(td)

    @pytest.mark.parametrize("clone", [True, False])
    def test_update(self, td_name, device, clone):
        td = getattr(self, td_name)(device)
        td.unlock_()  # make sure that the td is not locked
        keys = set(td.keys())
        td.update({"x": torch.zeros(td.shape)}, clone=clone)
        assert set(td.keys()) == keys.union({"x"})
        # now with nested: using tuples for keys
        td.update({("somenested", "z"): torch.zeros(td.shape)})
        assert td["somenested"].shape == td.shape
        assert td["somenested", "z"].shape == td.shape
        td.update({("somenested", "zz"): torch.zeros(td.shape)})
        assert td["somenested"].shape == td.shape
        assert td["somenested", "zz"].shape == td.shape
        # now with nested: using nested dicts
        td["newnested"] = {"z": torch.zeros(td.shape)}
        keys = set(td.keys(True))
        assert ("newnested", "z") in keys
        td.update({"newnested": {"y": torch.zeros(td.shape)}}, clone=clone)
        keys = keys.union({("newnested", "y")})
        assert keys == set(td.keys(True))
        td.update(
            {
                ("newnested", "x"): torch.zeros(td.shape),
                ("newnested", "w"): torch.zeros(td.shape),
            },
            clone=clone,
        )
        keys = keys.union({("newnested", "x"), ("newnested", "w")})
        assert keys == set(td.keys(True))
        td.update({("newnested",): {"v": torch.zeros(td.shape)}}, clone=clone)
        keys = keys.union(
            {
                ("newnested", "v"),
            }
        )
        assert keys == set(td.keys(True))

        if td_name in ("sub_td", "sub_td2"):
            with pytest.raises(ValueError, match="Tried to replace a tensordict with"):
                td.update({"newnested": torch.zeros(td.shape)}, clone=clone)
        else:
            td.update({"newnested": torch.zeros(td.shape)}, clone=clone)
            assert isinstance(td["newnested"], torch.Tensor)

    def test_update_at_(self, td_name, device):
        td = getattr(self, td_name)(device)
        td0 = td[1].clone().zero_()
        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="a view of a leaf Variable"):
                td.update_at_(td0, 0)
            return
        td.update_at_(td0, 0)
        assert (td[0] == 0).all()

    def test_write_on_subtd(self, td_name, device):
        td = getattr(self, td_name)(device)
        sub_td = td.get_sub_tensordict(0)
        # should not work with td_params
        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="a view of a leaf"):
                sub_td["a"] = torch.full((3, 2, 1, 5), 1.0, device=device)
            return
        sub_td["a"] = torch.full((3, 2, 1, 5), 1.0, device=device)
        assert (td["a"][0] == 1).all()

    def test_pad(self, td_name, device):
        td = getattr(self, td_name)(device)
        paddings = [
            [0, 1, 0, 2],
            [1, 0, 0, 2],
            [1, 0, 2, 1],
        ]

        for pad_size in paddings:
            padded_td = pad(td, pad_size)
            padded_td._check_batch_size()
            amount_expanded = [0] * (len(pad_size) // 2)
            for i in range(0, len(pad_size), 2):
                amount_expanded[i // 2] = pad_size[i] + pad_size[i + 1]

            for key in padded_td.keys():
                expected_dims = tuple(
                    sum(p)
                    for p in zip(
                        td[key].shape,
                        amount_expanded
                        + [0] * (len(td[key].shape) - len(amount_expanded)),
                    )
                )
                assert padded_td[key].shape == expected_dims

        with pytest.raises(RuntimeError):
            pad(td, [0] * 100)

        with pytest.raises(RuntimeError):
            pad(td, [0])

    def test_reshape(self, td_name, device):
        td = getattr(self, td_name)(device)
        td_reshape = td.reshape(td.shape)
        assert isinstance(td_reshape, TensorDict)
        assert td_reshape.shape.numel() == td.shape.numel()
        assert td_reshape.shape == td.shape
        td_reshape = td.reshape(*td.shape)
        assert isinstance(td_reshape, TensorDict)
        assert td_reshape.shape.numel() == td.shape.numel()
        assert td_reshape.shape == td.shape
        td_reshape = td.reshape(size=td.shape)
        assert isinstance(td_reshape, TensorDict)
        assert td_reshape.shape.numel() == td.shape.numel()
        assert td_reshape.shape == td.shape
        td_reshape = td.reshape(-1)
        assert isinstance(td_reshape, TensorDict)
        assert td_reshape.shape.numel() == td.shape.numel()
        assert td_reshape.shape == torch.Size([td.shape.numel()])
        td_reshape = td.reshape((-1,))
        assert isinstance(td_reshape, TensorDict)
        assert td_reshape.shape.numel() == td.shape.numel()
        assert td_reshape.shape == torch.Size([td.shape.numel()])
        td_reshape = td.reshape(size=(-1,))
        assert isinstance(td_reshape, TensorDict)
        assert td_reshape.shape.numel() == td.shape.numel()
        assert td_reshape.shape == torch.Size([td.shape.numel()])

    def test_view(self, td_name, device):
        if td_name in ("permute_td", "sub_td2"):
            pytest.skip("view incompatible with stride / permutation")
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()  # make sure that the td is not locked
        td_view = td.view(-1)
        tensor = td.get("a")
        tensor = tensor.view(-1, tensor.numel() // prod(td.batch_size))
        tensor = torch.ones_like(tensor)
        if td_name == "sub_td":
            td_view.set_("a", tensor)
        else:
            td_view.set("a", tensor)
        assert (td_view.get("a") == tensor).all()
        assert (td.get("a") == tensor.view(td.get("a").shape)).all()
        assert td_view.view(td.shape) is td
        assert td_view.view(*td.shape) is td
        assert (td_view.get("a") == 1).all()
        assert (td.get("a") == 1).all()

    def test_default_nested(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        default_val = torch.randn(())
        timbers = td.get(("shiver", "my", "timbers"), default_val)
        assert timbers == default_val

    def test_inferred_view_size(self, td_name, device):
        if td_name in ("permute_td", "sub_td2"):
            pytest.skip("view incompatible with stride / permutation")
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        for i in range(len(td.shape)):
            # replacing every index one at a time
            # with -1, to test that td.view(..., -1, ...)
            # always returns the original tensordict
            new_shape = [
                dim_size if dim_idx != i else -1
                for dim_idx, dim_size in enumerate(td.shape)
            ]
            assert td.view(-1).view(*new_shape) is td
            assert td.view(*new_shape) is td

    @pytest.mark.parametrize("dim", [0, 1, -1, -5])
    @pytest.mark.parametrize(
        "key", ["heterogeneous-entry", ("sub", "heterogeneous-entry")]
    )
    def test_nestedtensor_stack(self, td_name, device, dim, key):
        torch.manual_seed(1)
        td1 = getattr(self, td_name)(device).unlock_()
        td2 = getattr(self, td_name)(device).unlock_()

        td1[key] = torch.randn(*td1.shape, 2)
        td2[key] = torch.randn(*td1.shape, 3)
        td_stack = torch.stack([td1, td2], dim)
        # get will fail
        with pytest.raises(
            RuntimeError, match="Found more than one unique shape in the tensors"
        ):
            td_stack.get(key)
        with pytest.raises(
            RuntimeError, match="Found more than one unique shape in the tensors"
        ):
            td_stack[key]
        if dim in (0, -5):
            # this will work if stack_dim is 0 (or equivalently -self.batch_dims)
            # it is the proper way to get that entry
            td_stack.get_nestedtensor(key)
        else:
            # if the stack_dim is not zero, then calling get_nestedtensor is disallowed
            with pytest.raises(
                RuntimeError,
                match="LazyStackedTensorDict.get_nestedtensor can only be called "
                "when the stack_dim is 0.",
            ):
                td_stack.get_nestedtensor(key)
        with pytest.raises(
            RuntimeError, match="Found more than one unique shape in the tensors"
        ):
            td_stack.contiguous()
        with pytest.raises(
            RuntimeError, match="Found more than one unique shape in the tensors"
        ):
            td_stack.to_tensordict()
        # cloning is type-preserving: we can do that operation
        td_stack.clone()

    def test_clone_td(self, td_name, device, tmp_path):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_h5":
            # need a new file
            newfile = tmp_path / "file.h5"
            clone = td.clone(newfile=newfile)
        else:
            clone = torch.clone(td)
        assert (clone == td).all()
        assert td.batch_size == clone.batch_size
        assert type(td.clone(recurse=False)) is type(td)
        if td_name in (
            "stacked_td",
            "nested_stacked_td",
            "saved_td",
            "squeezed_td",
            "unsqueezed_td",
            "sub_td",
            "sub_td2",
            "permute_td",
            "td_h5",
        ):
            assert td.clone(recurse=False).get("a") is not td.get("a")
        else:
            assert td.clone(recurse=False).get("a") is td.get("a")

    def test_rename_key(self, td_name, device) -> None:
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td.is_locked:
            with pytest.raises(
                RuntimeError, match=re.escape(TensorDictBase.LOCK_ERROR)
            ):
                td.rename_key_("a", "b", safe=True)
        else:
            with pytest.raises(KeyError, match="already present in TensorDict"):
                td.rename_key_("a", "b", safe=True)
        a = td.get("a")
        if td.is_locked:
            with pytest.raises(RuntimeError, match="Cannot modify"):
                td.rename_key_("a", "z")
            return
        else:
            td.rename_key_("a", "z")
        with pytest.raises(KeyError):
            td.get("a")
        assert "a" not in td.keys()

        z = td.get("z")
        if isinstance(a, MemmapTensor):
            a = a._tensor
        if isinstance(z, MemmapTensor):
            z = z._tensor
        torch.testing.assert_close(a, z)

        new_z = torch.randn_like(z)
        if td_name in ("sub_td", "sub_td2"):
            td.set_("z", new_z)
        else:
            td.set("z", new_z)

        torch.testing.assert_close(new_z, td.get("z"))

        new_z = torch.randn_like(z)
        if td_name == "td_params":
            td.data.set_("z", new_z)
        else:
            td.set_("z", new_z)
        torch.testing.assert_close(new_z, td.get("z"))

    def test_rename_key_nested(self, td_name, device) -> None:
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()
        td["nested", "conflict"] = torch.zeros(td.shape)
        with pytest.raises(KeyError, match="already present in TensorDict"):
            td.rename_key_(("nested", "conflict"), "b", safe=True)
        td["nested", "first"] = torch.zeros(td.shape)
        td.rename_key_(("nested", "first"), "second")
        assert (td["second"] == 0).all()
        assert ("nested", "first") not in td.keys(True)
        td.rename_key_("second", ("nested", "back"))
        assert (td[("nested", "back")] == 0).all()
        assert "second" not in td.keys()

    def test_set_nontensor(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()
        r = torch.randn_like(td.get("a"))
        td.set("numpy", r.cpu().numpy())
        torch.testing.assert_close(td.get("numpy"), r)

    @pytest.mark.parametrize(
        "actual_index,expected_index",
        [
            (..., (slice(None),) * TD_BATCH_SIZE),
            ((..., 0), (slice(None),) * (TD_BATCH_SIZE - 1) + (0,)),
            ((0, ...), (0,) + (slice(None),) * (TD_BATCH_SIZE - 1)),
            ((0, ..., 0), (0,) + (slice(None),) * (TD_BATCH_SIZE - 2) + (0,)),
        ],
    )
    def test_getitem_ellipsis(self, td_name, device, actual_index, expected_index):
        torch.manual_seed(1)

        td = getattr(self, td_name)(device)

        actual_td = td[actual_index]
        expected_td = td[expected_index]
        other_expected_td = td.to_tensordict()[expected_index]
        assert expected_td.shape == _getitem_batch_size(
            td.batch_size, convert_ellipsis_to_idx(actual_index, td.batch_size)
        )
        assert other_expected_td.shape == actual_td.shape
        assert_allclose_td(actual_td, other_expected_td)
        assert_allclose_td(actual_td, expected_td)

    @pytest.mark.parametrize("actual_index", [..., (..., 0), (0, ...), (0, ..., 0)])
    def test_setitem_ellipsis(self, td_name, device, actual_index):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)

        idx = actual_index
        td_clone = td.clone()
        actual_td = td_clone[idx].clone()
        if td_name in ("td_params",):
            td_set = actual_td.apply(lambda x: x.data)
        else:
            td_set = actual_td
        td_set.zero_()

        for key in actual_td.keys():
            assert (actual_td.get(key) == 0).all()

        if td_name in ("td_params",):
            td_set = td_clone.data
        else:
            td_set = td_clone

        td_set[idx] = actual_td
        for key in td_clone.keys():
            assert (td_clone[idx].get(key) == 0).all()

    @pytest.mark.parametrize(
        "idx", [slice(1), torch.tensor([0]), torch.tensor([0, 1]), range(1), range(2)]
    )
    def test_setitem(self, td_name, device, idx):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if isinstance(idx, torch.Tensor) and idx.numel() > 1 and td.shape[0] == 1:
            pytest.mark.skip("cannot index tensor with desired index")
            return

        td_clone = td[idx].to_tensordict().zero_()
        if td_name == "td_params":
            td.data[idx] = td_clone
        else:
            td[idx] = td_clone
        assert (td[idx].get("a") == 0).all()

        td_clone = torch.cat([td_clone, td_clone], 0)
        with pytest.raises(
            RuntimeError,
            match=r"differs from the source batch size|batch dimension mismatch|Cannot broadcast the tensordict",
        ):
            td[idx] = td_clone

    def test_setitem_string(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()
        td["d"] = torch.randn(4, 3, 2, 1, 5)
        assert "d" in td.keys()

    def test_getitem_string(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert isinstance(td["a"], (MemmapTensor, torch.Tensor))

    def test_getitem_nestedtuple(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert isinstance(td[(("a",))], (MemmapTensor, torch.Tensor))
        assert isinstance(td.get((("a",))), (MemmapTensor, torch.Tensor))

    def test_setitem_nestedtuple(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td.is_locked:
            td.unlock_()
        td[" a ", (("little", "story")), "about", ("myself",)] = torch.zeros(td.shape)
        assert (td[" a ", "little", "story", "about", "myself"] == 0).all()

    def test_getitem_range(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert_allclose_td(td[range(2)], td[[0, 1]])
        if td_name not in ("td_h5",):
            # for h5, we can't use a double list index
            assert td[range(1), range(1)].shape == td[[0], [0]].shape
            assert_allclose_td(td[range(1), range(1)], td[[0], [0]])
        assert_allclose_td(td[:, range(2)], td[:, [0, 1]])
        assert_allclose_td(td[..., range(1)], td[..., [0]])

        if td_name in ("stacked_td", "nested_stacked_td"):
            # this is a bit contrived, but want to check that if we pass something
            # weird as the index to the stacking dimension we'll get the error
            idx = (slice(None),) * td.stack_dim + ({1, 2, 3},)
            with pytest.raises(TypeError, match="Invalid index"):
                td[idx]

    def test_setitem_nested_dict_value(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)

        # Create equivalent TensorDict and dict nested values for setitem
        nested_dict_value = {"e": torch.randn(4, 3, 2, 1, 10)}
        nested_tensordict_value = TensorDict(
            nested_dict_value, batch_size=td.batch_size, device=device
        )
        td_clone1 = td.clone(recurse=True)
        td_clone2 = td.clone(recurse=True)

        td_clone1["d"] = nested_dict_value
        td_clone2["d"] = nested_tensordict_value
        assert (td_clone1 == td_clone2).all()

    def test_transpose(self, td_name, device):
        td = getattr(self, td_name)(device)
        tdt = td.transpose(0, 1)
        assert tdt.shape == torch.Size([td.shape[1], td.shape[0], *td.shape[2:]])
        for key, value in tdt.items(True):
            assert value.shape == torch.Size(
                [td.get(key).shape[1], td.get(key).shape[0], *td.get(key).shape[2:]]
            )
        tdt = td.transpose(-1, -2)
        for key, value in tdt.items(True):
            assert value.shape == td.get(key).transpose(2, 3).shape
        if td_name in ("td_params",):
            assert tdt.transpose(-1, -2)._param_td is td._param_td
        else:
            assert tdt.transpose(-1, -2) is td
        with td.unlock_():
            tdt.set(("some", "transposed", "tensor"), torch.zeros(tdt.shape))
        assert td.get(("some", "transposed", "tensor")).shape == td.shape
        if td_name in ("td_params",):
            assert td.transpose(0, 0)._param_td is td._param_td
        else:
            assert td.transpose(0, 0) is td
        with pytest.raises(
            ValueError, match="The provided dimensions are incompatible"
        ):
            td.transpose(-5, -6)
        with pytest.raises(
            ValueError, match="The provided dimensions are incompatible"
        ):
            tdt.transpose(-5, -6)

    def test_create_nested(self, td_name, device):
        td = getattr(self, td_name)(device)
        with td.unlock_():
            td.create_nested("root")
            assert td.get("root").shape == td.shape
            assert is_tensor_collection(td.get("root"))
            td.create_nested(("some", "nested", "key"))
            assert td.get(("some", "nested", "key")).shape == td.shape
            assert is_tensor_collection(td.get(("some", "nested", "key")))
        if td_name in ("sub_td", "sub_td2"):
            return
        with td.lock_(), pytest.raises(RuntimeError):
            td.create_nested("root")

    def test_tensordict_set(self, td_name, device):
        torch.manual_seed(1)
        np.random.seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()

        # test set
        val1 = np.ones(shape=(4, 3, 2, 1, 10))
        td.set("key1", val1)
        assert (td.get("key1") == 1).all()
        with pytest.raises(RuntimeError):
            td.set("key1", np.ones(shape=(5, 10)))

        # test set_
        val2 = np.zeros(shape=(4, 3, 2, 1, 10))
        td.set_("key1", val2)
        assert (td.get("key1") == 0).all()
        if td_name not in ("stacked_td", "nested_stacked_td"):
            err_msg = r"key.*smartypants.*not found in "
        elif td_name in ("td_h5",):
            err_msg = "Unable to open object"
        else:
            err_msg = "setting a value in-place on a stack of TensorDict"

        with pytest.raises(KeyError, match=err_msg):
            td.set_("smartypants", np.ones(shape=(4, 3, 2, 1, 5)))

        # test set_at_
        td.set("key2", np.random.randn(4, 3, 2, 1, 5))
        x = np.ones(shape=(2, 1, 5)) * 42
        td.set_at_("key2", x, (2, 2))
        assert (td.get("key2")[2, 2] == 42).all()

    def test_tensordict_set_dict_value(self, td_name, device):
        torch.manual_seed(1)
        np.random.seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()

        # test set
        val1 = {"subkey1": torch.ones(4, 3, 2, 1, 10)}
        td.set("key1", val1)
        assert (td.get("key1").get("subkey1") == 1).all()
        with pytest.raises(RuntimeError):
            td.set("key1", torch.ones(5, 10))

        # test set_
        val2 = {"subkey1": torch.zeros(4, 3, 2, 1, 10)}
        if td_name in ("td_params",):
            td.data.set_("key1", val2)
        else:
            td.set_("key1", val2)
        assert (td.get("key1").get("subkey1") == 0).all()

        if td_name not in ("stacked_td", "nested_stacked_td"):
            err_msg = r"key.*smartypants.*not found in "
        elif td_name in ("td_h5",):
            err_msg = "Unable to open object"
        else:
            err_msg = "setting a value in-place on a stack of TensorDict"

        with pytest.raises(KeyError, match=err_msg):
            td.set_("smartypants", np.ones(shape=(4, 3, 2, 1, 5)))

    def test_delitem(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name in ("memmap_td",):
            with pytest.raises(RuntimeError, match="Cannot modify"):
                del td["a"]
            return
        del td["a"]
        assert "a" not in td.keys()

    def test_to_dict_nested(self, td_name, device):
        def recursive_checker(cur_dict):
            for _, value in cur_dict.items():
                if isinstance(value, TensorDict):
                    return False
                elif isinstance(value, dict) and not recursive_checker(value):
                    return False
            return True

        td = getattr(self, td_name)(device)
        td.unlock_()

        # Create nested TensorDict
        nested_tensordict_value = TensorDict(
            {"e": torch.randn(4, 3, 2, 1, 10)}, batch_size=td.batch_size, device=device
        )
        td["d"] = nested_tensordict_value

        # Convert into dictionary and recursively check if the values are TensorDicts
        td_dict = td.to_dict()
        assert recursive_checker(td_dict)

    @pytest.mark.parametrize(
        "index", ["tensor1", "mask", "int", "range", "tensor2", "slice_tensor"]
    )
    def test_update_subtensordict(self, td_name, device, index):
        td = getattr(self, td_name)(device)
        if index == "mask":
            index = torch.zeros(td.shape[0], dtype=torch.bool, device=device)
            index[-1] = 1
        elif index == "int":
            index = td.shape[0] - 1
        elif index == "range":
            index = range(td.shape[0] - 1, td.shape[0])
        elif index == "tensor1":
            index = torch.tensor(td.shape[0] - 1, device=device)
        elif index == "tensor2":
            index = torch.tensor([td.shape[0] - 2, td.shape[0] - 1], device=device)
        elif index == "slice_tensor":
            index = (
                slice(None),
                torch.tensor([td.shape[1] - 2, td.shape[1] - 1], device=device),
            )

        sub_td = td.get_sub_tensordict(index)
        assert sub_td.shape == td.to_tensordict()[index].shape
        assert sub_td.shape == td[index].shape, (td, index)
        td0 = td[index]
        td0 = td0.to_tensordict()
        td0 = td0.apply(lambda x: x * 0 + 2)
        assert sub_td.shape == td0.shape
        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="a leaf Variable"):
                sub_td.update(td0)
            return
        sub_td.update(td0)
        assert (sub_td == 2).all()
        assert (td[index] == 2).all()

    @pytest.mark.filterwarnings("error")
    def test_stack_onto(self, td_name, device, tmpdir):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if td_name == "td_h5":
            td0 = td.clone(newfile=tmpdir / "file0.h5").apply_(lambda x: x.zero_())
            td1 = td.clone(newfile=tmpdir / "file1.h5").apply_(lambda x: x.zero_() + 1)
        else:
            td0 = td.clone()
            if td_name in ("td_params",):
                td0.data.apply_(lambda x: x.zero_())
            else:
                td0.apply_(lambda x: x.zero_())
            td1 = td.clone()
            if td_name in ("td_params",):
                td1.data.apply_(lambda x: x.zero_() + 1)
            else:
                td1.apply_(lambda x: x.zero_() + 1)

        td_out = td.unsqueeze(1).expand(td.shape[0], 2, *td.shape[1:]).clone()
        td_stack = torch.stack([td0, td1], 1)
        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="out.batch_size and stacked"):
                torch.stack([td0, td1], 0, out=td_out)
            return
        data_ptr_set_before = {val.data_ptr() for val in decompose(td_out)}
        torch.stack([td0, td1], 1, out=td_out)
        data_ptr_set_after = {val.data_ptr() for val in decompose(td_out)}
        assert data_ptr_set_before == data_ptr_set_after
        assert (td_stack == td_out).all()

    @pytest.mark.filterwarnings("error")
    def test_stack_tds_on_subclass(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        tds_count = td.batch_size[0]
        tds_batch_size = td.batch_size[1:]
        tds_list = [
            TensorDict(
                source={
                    "a": torch.ones(*tds_batch_size, 5),
                    "b": torch.ones(*tds_batch_size, 10),
                    "c": torch.ones(*tds_batch_size, 3, dtype=torch.long),
                },
                batch_size=tds_batch_size,
                device=device,
            )
            for _ in range(tds_count)
        ]
        if td_name in ("sub_td", "sub_td2"):
            with pytest.raises(IndexError, match="storages of the indexed tensors"):
                torch.stack(tds_list, 0, out=td)
            return
        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="arguments don't support automatic"):
                torch.stack(tds_list, 0, out=td)
            return
        data_ptr_set_before = {val.data_ptr() for val in decompose(td)}

        stacked_td = torch.stack(tds_list, 0, out=td)
        data_ptr_set_after = {val.data_ptr() for val in decompose(td)}
        assert data_ptr_set_before == data_ptr_set_after
        assert stacked_td.batch_size == td.batch_size
        assert stacked_td is td
        for key in ("a", "b", "c"):
            assert (stacked_td[key] == 1).all()

    @pytest.mark.filterwarnings("error")
    def test_stack_subclasses_on_td(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td = td.expand(3, *td.batch_size).clone().zero_()
        tds_list = [getattr(self, td_name)(device) for _ in range(3)]
        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="arguments don't support automatic"):
                torch.stack(tds_list, 0, out=td)
            return
        data_ptr_set_before = {val.data_ptr() for val in decompose(td)}
        stacked_td = stack_td(tds_list, 0, out=td)
        data_ptr_set_after = {val.data_ptr() for val in decompose(td)}
        assert data_ptr_set_before == data_ptr_set_after
        assert stacked_td.batch_size == td.batch_size
        for key in ("a", "b", "c"):
            assert (stacked_td[key] == td[key]).all()

    @pytest.mark.parametrize("dim", [0, 1])
    @pytest.mark.parametrize("chunks", [1, 2])
    def test_chunk(self, td_name, device, dim, chunks):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        if len(td.shape) - 1 < dim:
            pytest.mark.skip(f"no dim {dim} in td")
            return

        chunks = min(td.shape[dim], chunks)
        td_chunks = td.chunk(chunks, dim)
        assert len(td_chunks) == chunks
        assert sum([_td.shape[dim] for _td in td_chunks]) == td.shape[dim]
        assert (torch.cat(td_chunks, dim) == td).all()

    def test_as_tensor(self, td_name, device):
        td = getattr(self, td_name)(device)
        if "memmap" in td_name and device == torch.device("cpu"):
            tdt = td.as_tensor()
            assert (tdt == td).all()
        elif "memmap" in td_name:
            with pytest.raises(
                RuntimeError, match="can only be called with MemmapTensors stored"
            ):
                td.as_tensor()
        else:
            # checks that it runs
            td.as_tensor()

    def test_items_values_keys(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()
        keys = list(td.keys())
        values = list(td.values())
        items = list(td.items())

        # Test td.items()
        constructed_td1 = TensorDict({}, batch_size=td.shape)
        for key, value in items:
            constructed_td1.set(key, value)

        assert (td == constructed_td1).all()

        # Test td.keys() and td.values()
        # items = [key, value] should be verified
        assert len(values) == len(items)
        assert len(keys) == len(items)
        constructed_td2 = TensorDict({}, batch_size=td.shape)
        for key, value in list(zip(td.keys(), td.values())):
            constructed_td2.set(key, value)

        assert (td == constructed_td2).all()

        # Test that keys is sorted
        assert all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1))

        # Add new element to tensor
        a = td.get("a")
        td.set("x", torch.randn_like(a))
        keys = list(td.keys())
        values = list(td.values())
        items = list(td.items())

        # Test that keys is still sorted after adding the element
        assert all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1))

        # Test td.items()
        # after adding the new element
        constructed_td1 = TensorDict({}, batch_size=td.shape)
        for key, value in items:
            constructed_td1.set(key, value)

        assert (td == constructed_td1).all()

        # Test td.keys() and td.values()
        # items = [key, value] should be verified
        # even after adding the new element
        assert len(values) == len(items)
        assert len(keys) == len(items)

        constructed_td2 = TensorDict({}, batch_size=td.shape)
        for key, value in list(zip(td.keys(), td.values())):
            constructed_td2.set(key, value)

        assert (td == constructed_td2).all()

    def test_set_requires_grad(self, td_name, device):
        td = getattr(self, td_name)(device)
        if td_name in ("td_params",):
            td.apply(lambda x: x.requires_grad_(False))
        td.unlock_()
        assert not td.get("a").requires_grad
        if td_name in ("td_h5",):
            with pytest.raises(
                RuntimeError, match="Cannot set a tensor that has requires_grad=True"
            ):
                td.set("a", torch.randn_like(td.get("a")).requires_grad_())
            return
        if td_name in ("sub_td", "sub_td2"):
            td.set_("a", torch.randn_like(td.get("a")).requires_grad_())
        else:
            td.set("a", torch.randn_like(td.get("a")).requires_grad_())

        assert td.get("a").requires_grad

    def test_nested_td_emptyshape(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()
        tdin = TensorDict({"inner": torch.randn(*td.shape, 1)}, [], device=device)
        td["inner_td"] = tdin
        tdin.batch_size = td.batch_size
        assert (td["inner_td"] == tdin).all()

    def test_nested_td(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()
        tdin = TensorDict({"inner": torch.randn(td.shape)}, td.shape, device=device)
        td.set("inner_td", tdin)
        assert (td["inner_td"] == tdin).all()

    def test_nested_dict_init(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td.unlock_()

        # Create TensorDict and dict equivalent values, and populate each with according nested value
        td_clone = td.clone(recurse=True)
        td_dict = td.to_dict()
        nested_dict_value = {"e": torch.randn(4, 3, 2, 1, 10)}
        nested_tensordict_value = TensorDict(
            nested_dict_value, batch_size=td.batch_size, device=device
        )
        td_dict["d"] = nested_dict_value
        td_clone["d"] = nested_tensordict_value

        # Re-init new TensorDict from dict, and check if they're equal
        td_dict_init = TensorDict(td_dict, batch_size=td.batch_size, device=device)

        assert (td_clone == td_dict_init).all()

    def test_nested_td_index(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()

        sub_td = TensorDict({}, [*td.shape, 2], device=device)
        a = torch.zeros([*td.shape, 2, 2], device=device)
        sub_sub_td = TensorDict({"a": a}, [*td.shape, 2, 2], device=device)
        sub_td.set("sub_sub_td", sub_sub_td)
        td.set("sub_td", sub_td)
        assert (td["sub_td", "sub_sub_td", "a"] == 0).all()
        assert (
            td["sub_td"]["sub_sub_td"]["a"] == td["sub_td", "sub_sub_td", "a"]
        ).all()

        a = torch.ones_like(a)
        other_sub_sub_td = TensorDict({"a": a}, [*td.shape, 2, 2])
        td["sub_td", "sub_sub_td"] = other_sub_sub_td
        assert (td["sub_td", "sub_sub_td", "a"] == 1).all()
        assert (
            td["sub_td"]["sub_sub_td"]["a"] == td["sub_td", "sub_sub_td", "a"]
        ).all()

        b = torch.ones_like(a)
        other_sub_sub_td = TensorDict({"b": b}, [*td.shape, 2, 2])

        if td_name in ("sub_td", "sub_td2"):
            td["sub_td", "sub_sub_td"] = other_sub_sub_td
        else:
            td["sub_td", "sub_sub_td"] = other_sub_sub_td
            assert (td["sub_td", "sub_sub_td", "b"] == 1).all()
            assert (
                td["sub_td"]["sub_sub_td"]["b"] == td["sub_td", "sub_sub_td", "b"]
            ).all()

    @pytest.mark.parametrize("inplace", [True, False])
    @pytest.mark.parametrize("separator", [",", "-"])
    def test_flatten_keys(self, td_name, device, inplace, separator):
        td = getattr(self, td_name)(device)
        locked = td.is_locked
        td.unlock_()
        nested_nested_tensordict = TensorDict(
            {
                "a": torch.zeros(*td.shape, 2, 3),
            },
            [*td.shape, 2],
        )
        nested_tensordict = TensorDict(
            {
                "a": torch.zeros(*td.shape, 2),
                "nested_nested_tensordict": nested_nested_tensordict,
            },
            td.shape,
        )
        td["nested_tensordict"] = nested_tensordict
        if locked:
            td.lock_()

        if inplace and locked:
            with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
                td_flatten = td.flatten_keys(inplace=inplace, separator=separator)
            return
        else:
            td_flatten = td.flatten_keys(inplace=inplace, separator=separator)
        for value in td_flatten.values():
            assert not isinstance(value, TensorDictBase)
        assert (
            separator.join(["nested_tensordict", "nested_nested_tensordict", "a"])
            in td_flatten.keys()
        )
        if inplace:
            assert td_flatten is td
        else:
            assert td_flatten is not td

    @pytest.mark.parametrize("inplace", [True, False])
    @pytest.mark.parametrize("separator", [",", "-"])
    def test_unflatten_keys(self, td_name, device, inplace, separator):
        td = getattr(self, td_name)(device)
        locked = td.is_locked
        td.unlock_()
        nested_nested_tensordict = TensorDict(
            {
                "a": torch.zeros(*td.shape, 2, 3),
            },
            [*td.shape, 2],
        )
        nested_tensordict = TensorDict(
            {
                "a": torch.zeros(*td.shape, 2),
                "nested_nested_tensordict": nested_nested_tensordict,
            },
            td.shape,
        )
        td["nested_tensordict"] = nested_tensordict

        if inplace and locked:
            td_flatten = td.flatten_keys(inplace=inplace, separator=separator)
            td_flatten.lock_()
            with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
                td_unflatten = td_flatten.unflatten_keys(
                    inplace=inplace, separator=separator
                )
            return
        else:
            if locked:
                td.lock_()
            td_flatten = td.flatten_keys(inplace=inplace, separator=separator)
            td_unflatten = td_flatten.unflatten_keys(
                inplace=inplace, separator=separator
            )
        assert (td == td_unflatten).all()
        if inplace:
            assert td is td_unflatten

    def test_repr(self, td_name, device):
        td = getattr(self, td_name)(device)
        _ = str(td)

    def test_memmap_(self, td_name, device):
        td = getattr(self, td_name)(device)
        if td_name in ("sub_td", "sub_td2"):
            with pytest.raises(
                RuntimeError,
                match="Converting a sub-tensordict values to memmap cannot be done",
            ):
                td.memmap_()
        elif td_name in ("td_h5", "td_params"):
            with pytest.raises(
                RuntimeError,
                match="Cannot build a memmap TensorDict in-place",
            ):
                td.memmap_()
        else:
            td.memmap_()
            assert td.is_memmap()

    def test_memmap_like(self, td_name, device):
        td = getattr(self, td_name)(device)
        tdmemmap = td.memmap_like()
        assert tdmemmap is not td
        for key in td.keys(True):
            assert td[key] is not tdmemmap[key]
        assert (tdmemmap == 0).all()

    def test_memmap_prefix(self, td_name, device, tmp_path):
        if td_name == "memmap_td":
            pytest.skip(
                "Memmap case is redundant, functionality checked by other cases"
            )

        td = getattr(self, td_name)(device)
        if td_name in ("sub_td", "sub_td2"):
            with pytest.raises(
                RuntimeError,
                match="Converting a sub-tensordict values to memmap cannot be done",
            ):
                td.memmap_(tmp_path / "tensordict")
            return
        elif td_name in ("td_h5", "td_params"):
            with pytest.raises(
                RuntimeError,
                match="Cannot build a memmap TensorDict in-place",
            ):
                td.memmap_(tmp_path / "tensordict")
            return
        else:
            td.memmap_(tmp_path / "tensordict")

        assert (tmp_path / "tensordict" / "meta.pt").exists()
        metadata = torch.load(tmp_path / "tensordict" / "meta.pt")
        if td_name in ("stacked_td", "nested_stacked_td"):
            pass
        elif td_name in ("unsqueezed_td", "squeezed_td", "permute_td"):
            assert metadata["batch_size"] == td._source.batch_size
            assert metadata["device"] == td._source.device
        else:
            assert metadata["batch_size"] == td.batch_size
            assert metadata["device"] == td.device

        td2 = td.__class__.load_memmap(tmp_path / "tensordict")
        assert (td == td2).all()

    @pytest.mark.parametrize("copy_existing", [False, True])
    def test_memmap_existing(self, td_name, device, copy_existing, tmp_path):
        if td_name == "memmap_td":
            pytest.skip(
                "Memmap case is redundant, functionality checked by other cases"
            )
        elif td_name in ("sub_td", "sub_td2", "td_h5", "td_params"):
            pytest.skip(
                "SubTensorDict/H5 and memmap_ incompatibility is checked elsewhere"
            )

        td = getattr(self, td_name)(device).memmap_(prefix=tmp_path / "tensordict")
        td2 = getattr(self, td_name)(device).memmap_()

        if copy_existing:
            td3 = td.memmap_(prefix=tmp_path / "tensordict2", copy_existing=True)
            assert (td == td3).all()
        else:
            with pytest.raises(
                RuntimeError, match="TensorDict already contains MemmapTensors"
            ):
                # calling memmap_ with prefix that is different to contents gives error
                td.memmap_(prefix=tmp_path / "tensordict2")

            # calling memmap_ without prefix means no-op, regardless of whether contents
            # were saved in temporary or designated location (td vs. td2 resp.)
            td3 = td.memmap_()
            td4 = td2.memmap_()

            if td_name in ("stacked_td", "nested_stacked_td"):
                assert all(
                    all(
                        td3_[key] is value
                        for key, value in td_.items(
                            include_nested=True, leaves_only=True
                        )
                    )
                    for td_, td3_ in zip(td.tensordicts, td3.tensordicts)
                )
                assert all(
                    all(
                        td4_[key] is value
                        for key, value in td2_.items(
                            include_nested=True, leaves_only=True
                        )
                    )
                    for td2_, td4_ in zip(td2.tensordicts, td4.tensordicts)
                )
            elif td_name in ("permute_td", "squeezed_td", "unsqueezed_td"):
                assert all(
                    td3._source[key] is value
                    for key, value in td._source.items(
                        include_nested=True, leaves_only=True
                    )
                )
                assert all(
                    td4._source[key] is value
                    for key, value in td2._source.items(
                        include_nested=True, leaves_only=True
                    )
                )
            else:
                assert all(
                    td3[key] is value
                    for key, value in td.items(include_nested=True, leaves_only=True)
                )
                assert all(
                    td4[key] is value
                    for key, value in td2.items(include_nested=True, leaves_only=True)
                )

    def test_setdefault_missing_key(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()
        expected = torch.ones_like(td.get("a"))
        inserted = td.setdefault("z", expected)
        assert (inserted == expected).all()

    def test_setdefault_existing_key(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()
        expected = td.get("a")
        inserted = td.setdefault("a", torch.ones_like(td.get("b")))
        assert (inserted == expected).all()

    def test_setdefault_nested(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.unlock_()

        tensor = torch.randn(4, 3, 2, 1, 5, device=device)
        tensor2 = torch.ones(4, 3, 2, 1, 5, device=device)
        sub_sub_tensordict = TensorDict({"c": tensor}, [4, 3, 2, 1], device=device)
        sub_tensordict = TensorDict(
            {"b": sub_sub_tensordict}, [4, 3, 2, 1], device=device
        )
        if td_name == "td_h5":
            del td["a"]
        if td_name == "sub_td":
            td = td._source.set(
                "a", sub_tensordict.expand(2, *sub_tensordict.shape)
            ).get_sub_tensordict(1)
        elif td_name == "sub_td2":
            td = td._source.set(
                "a",
                sub_tensordict.expand(2, *sub_tensordict.shape).permute(1, 0, 2, 3, 4),
            ).get_sub_tensordict((slice(None), 1))
        else:
            td.set("a", sub_tensordict)

        # if key exists we return the existing value
        torch.testing.assert_close(td.setdefault(("a", "b", "c"), tensor2), tensor)

        if not td_name == "stacked_td":
            torch.testing.assert_close(td.setdefault(("a", "b", "d"), tensor2), tensor2)
            torch.testing.assert_close(td.get(("a", "b", "d")), tensor2)

    @pytest.mark.parametrize("performer", ["torch", "tensordict"])
    def test_split(self, td_name, device, performer):
        td = getattr(self, td_name)(device)

        for dim in range(td.batch_dims):
            rep, remainder = divmod(td.shape[dim], 2)
            length = rep + remainder

            # split_sizes to be [2, 2, ..., 2, 1] or [2, 2, ..., 2]
            split_sizes = [2] * rep + [1] * remainder
            for test_split_size in (2, split_sizes):
                if performer == "torch":
                    tds = torch.split(td, test_split_size, dim)
                elif performer == "tensordict":
                    tds = td.split(test_split_size, dim)
                assert len(tds) == length

                for idx, split_td in enumerate(tds):
                    expected_split_dim_size = 1 if idx == rep else 2
                    expected_batch_size = [
                        expected_split_dim_size if dim_idx == dim else dim_size
                        for (dim_idx, dim_size) in enumerate(td.batch_size)
                    ]

                    # Test each split_td has the expected batch_size
                    assert split_td.batch_size == torch.Size(expected_batch_size)

                    if td_name == "nested_td":
                        assert isinstance(split_td["my_nested_td"], TensorDict)
                        assert isinstance(
                            split_td["my_nested_td"]["inner"], torch.Tensor
                        )

                    # Test each tensor (or nested_td) in split_td has the expected shape
                    for key, item in split_td.items():
                        expected_shape = [
                            expected_split_dim_size if dim_idx == dim else dim_size
                            for (dim_idx, dim_size) in enumerate(td[key].shape)
                        ]
                        assert item.shape == torch.Size(expected_shape)

                        if key == "my_nested_td":
                            expected_inner_tensor_size = [
                                expected_split_dim_size if dim_idx == dim else dim_size
                                for (dim_idx, dim_size) in enumerate(
                                    td[key]["inner"].shape
                                )
                            ]
                            assert item["inner"].shape == torch.Size(
                                expected_inner_tensor_size
                            )

    def test_pop(self, td_name, device):
        td = getattr(self, td_name)(device)
        assert "a" in td.keys()
        a = td["a"].clone()
        with td.unlock_():
            out = td.pop("a")
            assert (out == a).all()
            assert "a" not in td.keys()

            assert "b" in td.keys()
            b = td["b"].clone()
            default = torch.zeros_like(b).to(device)
            assert (default != b).all()
            out = td.pop("b", default)

            assert torch.ne(out, default).all()
            assert (out == b).all()

            assert "z" not in td.keys()
            out = td.pop("z", default)
            assert (out == default).all()

            with pytest.raises(
                KeyError,
                match=re.escape(r"You are trying to pop key"),
            ):
                td.pop("z")

    def test_setitem_slice(self, td_name, device):
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:] = td.clone()
        td_set[:1] = td[:1].clone().zero_()
        assert (td[:1] == 0).all()
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1] = td[:1].to_tensordict().zero_()
        assert (td[:1] == 0).all()

        # with broadcast
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1] = td[0].clone().zero_()
        assert (td[:1] == 0).all()
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1] = td[0].to_tensordict().zero_()
        assert (td[:1] == 0).all()

        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1, 0] = td[0, 0].clone().zero_()
        assert (td[:1, 0] == 0).all()
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1, 0] = td[0, 0].to_tensordict().zero_()
        assert (td[:1, 0] == 0).all()

        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1, :, 0] = td[0, :, 0].clone().zero_()
        assert (td[:1, :, 0] == 0).all()
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            td_set = td.data
        else:
            td_set = td
        td_set[:1, :, 0] = td[0, :, 0].to_tensordict().zero_()
        assert (td[:1, :, 0] == 0).all()

    def test_casts(self, td_name, device):
        td = getattr(self, td_name)(device)
        tdfloat = td.float()
        assert all(value.dtype is torch.float for value in tdfloat.values(True, True))
        tddouble = td.double()
        assert all(value.dtype is torch.double for value in tddouble.values(True, True))
        tdbfloat16 = td.bfloat16()
        assert all(
            value.dtype is torch.bfloat16 for value in tdbfloat16.values(True, True)
        )
        tdhalf = td.half()
        assert all(value.dtype is torch.half for value in tdhalf.values(True, True))
        tdint = td.int()
        assert all(value.dtype is torch.int for value in tdint.values(True, True))
        tdint = td.type(torch.int)
        assert all(value.dtype is torch.int for value in tdint.values(True, True))

    def test_empty_like(self, td_name, device):
        if "sub_td" in td_name:
            # we do not call skip to avoid systematic skips in internal code base
            return
        td = getattr(self, td_name)(device)
        if isinstance(td, _CustomOpTensorDict):
            # we do not call skip to avoid systematic skips in internal code base
            return
        td_empty = torch.empty_like(td)
        if td_name == "td_params":
            with pytest.raises(ValueError, match="Failed to update"):
                td.apply_(lambda x: x + 1.0)
            return

        td.apply_(lambda x: x + 1.0)
        assert type(td) is type(td_empty)
        assert all(val.any() for val in (td != td_empty).values(True, True))

    @pytest.mark.parametrize("nested", [False, True])
    def test_add_batch_dim_cache(self, td_name, device, nested):
        td = getattr(self, td_name)(device)
        if nested:
            td = TensorDict({"parent": td}, td.batch_size)
        from tensordict.nn import TensorDictModule  # noqa
        from torch import vmap

        fun = vmap(lambda x: x)
        if td_name == "td_h5":
            with pytest.raises(
                RuntimeError, match="Persistent tensordicts cannot be used with vmap"
            ):
                fun(td)
            return
        if td_name == "memmap_td" and device.type != "cpu":
            with pytest.raises(
                RuntimeError,
                match="MemmapTensor with non-cpu device are not supported in vmap ops",
            ):
                fun(td)
            return
        fun(td)

        if td_name == "td_params":
            with pytest.raises(RuntimeError, match="leaf Variable that requires grad"):
                td.zero_()
            return

        td.zero_()
        # this value should be cached
        std = fun(td)
        for value in std.values(True, True):
            assert (value == 0).all()


@pytest.mark.parametrize("device", [None, *get_available_devices()])
@pytest.mark.parametrize("dtype", [torch.float32, torch.uint8])
class TestTensorDictRepr:
    def td(self, device, dtype):
        if device is not None:
            device_not_none = device
        elif torch.has_cuda and torch.cuda.device_count():
            device_not_none = torch.device("cuda:0")
        else:
            device_not_none = torch.device("cpu")

        return TensorDict(
            source={
                "a": torch.zeros(4, 3, 2, 1, 5, dtype=dtype, device=device_not_none)
            },
            batch_size=[4, 3, 2, 1],
            device=device,
        )

    def nested_td(self, device, dtype):
        if device is not None:
            device_not_none = device
        elif torch.has_cuda and torch.cuda.device_count():
            device_not_none = torch.device("cuda:0")
        else:
            device_not_none = torch.device("cpu")
        return TensorDict(
            source={
                "my_nested_td": self.td(device, dtype),
                "b": torch.zeros(4, 3, 2, 1, 5, dtype=dtype, device=device_not_none),
            },
            batch_size=[4, 3, 2, 1],
            device=device,
        )

    def nested_tensorclass(self, device, dtype):
        from tensordict import tensorclass

        @tensorclass
        class MyClass:
            X: torch.Tensor
            y: "MyClass"
            z: str

        if device is not None:
            device_not_none = device
        elif torch.has_cuda and torch.cuda.device_count():
            device_not_none = torch.device("cuda:0")
        else:
            device_not_none = torch.device("cpu")
        nested_class = MyClass(
            X=torch.zeros(4, 3, 2, 1, dtype=dtype, device=device_not_none),
            y=MyClass(
                X=torch.zeros(4, 3, 2, 1, dtype=dtype, device=device_not_none),
                y=None,
                z=None,
                batch_size=[4, 3, 2, 1],
            ),
            z="z",
            batch_size=[4, 3, 2, 1],
        )
        return TensorDict(
            source={
                "my_nested_td": nested_class,
                "b": torch.zeros(4, 3, 2, 1, 5, dtype=dtype, device=device_not_none),
            },
            batch_size=[4, 3, 2, 1],
            device=device,
        )

    def stacked_td(self, device, dtype):
        if device is not None:
            device_not_none = device
        elif torch.has_cuda and torch.cuda.device_count():
            device_not_none = torch.device("cuda:0")
        else:
            device_not_none = torch.device("cpu")
        td1 = TensorDict(
            source={
                "a": torch.zeros(4, 3, 1, 5, dtype=dtype, device=device_not_none),
                "c": torch.zeros(4, 3, 1, 5, dtype=dtype, device=device_not_none),
            },
            batch_size=[4, 3, 1],
            device=device,
        )
        td2 = TensorDict(
            source={
                "a": torch.zeros(4, 3, 1, 5, dtype=dtype, device=device_not_none),
                "b": torch.zeros(4, 3, 1, 10, dtype=dtype, device=device_not_none),
            },
            batch_size=[4, 3, 1],
            device=device,
        )

        return stack_td([td1, td2], 2)

    def memmap_td(self, device, dtype):
        return self.td(device, dtype).memmap_()

    def share_memory_td(self, device, dtype):
        return self.td(device, dtype).share_memory_()

    def test_repr_plain(self, device, dtype):
        tensordict = self.td(device, dtype)
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_device = device if device else tensordict["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""TensorDict(
    fields={{
        a: Tensor(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        assert repr(tensordict) == expected

    def test_repr_memmap(self, device, dtype):
        tensordict = self.memmap_td(device, dtype)
        is_shared = False
        tensor_device = device if device else tensordict["a"].device
        is_shared_tensor = False
        expected = f"""TensorDict(
    fields={{
        a: MemmapTensor(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        assert repr(tensordict) == expected

    def test_repr_share_memory(self, device, dtype):
        tensordict = self.share_memory_td(device, dtype)
        is_shared = True
        tensor_class = "Tensor"
        tensor_device = device if device else tensordict["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""TensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        assert repr(tensordict) == expected

    def test_repr_nested(self, device, dtype):
        nested_td = self.nested_td(device, dtype)
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        tensor_device = device if device else nested_td["b"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""TensorDict(
    fields={{
        b: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        my_nested_td: TensorDict(
            fields={{
                a: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
            batch_size=torch.Size([4, 3, 2, 1]),
            device={str(device)},
            is_shared={is_shared})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        assert repr(nested_td) == expected

    def test_repr_nested_update(self, device, dtype):
        nested_td = self.nested_td(device, dtype)
        nested_td["my_nested_td"].rename_key_("a", "z")
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        tensor_device = device if device else nested_td["b"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""TensorDict(
    fields={{
        b: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        my_nested_td: TensorDict(
            fields={{
                z: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
            batch_size=torch.Size([4, 3, 2, 1]),
            device={str(device)},
            is_shared={is_shared})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        assert repr(nested_td) == expected

    def test_repr_stacked(self, device, dtype):
        stacked_td = self.stacked_td(device, dtype)
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        tensor_device = device if device else stacked_td["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""LazyStackedTensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    exclusive_fields={{
        0 ->
            c: {tensor_class}(shape=torch.Size([4, 3, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        1 ->
            b: {tensor_class}(shape=torch.Size([4, 3, 1, 10]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared},
    stack_dim={stacked_td.stack_dim})"""
        assert repr(stacked_td) == expected

    def test_repr_stacked_het(self, device, dtype):
        stacked_td = torch.stack(
            [
                TensorDict(
                    {
                        "a": torch.zeros(3, dtype=dtype),
                        "b": torch.zeros(2, 3, dtype=dtype),
                    },
                    [],
                    device=device,
                ),
                TensorDict(
                    {
                        "a": torch.zeros(2, dtype=dtype),
                        "b": torch.zeros((), dtype=dtype),
                    },
                    [],
                    device=device,
                ),
            ]
        )
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_device = device if device else torch.device("cpu")
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""LazyStackedTensorDict(
    fields={{
        a: Tensor(shape=torch.Size([2, -1]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        b: Tensor(shape=torch.Size([-1]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    exclusive_fields={{
    }},
    batch_size=torch.Size([2]),
    device={str(device)},
    is_shared={is_shared},
    stack_dim={stacked_td.stack_dim})"""
        assert repr(stacked_td) == expected

    @pytest.mark.parametrize("index", [None, (slice(None), 0)])
    def test_repr_indexed_tensordict(self, device, dtype, index):
        tensordict = self.td(device, dtype)[index]
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        tensor_device = device if device else tensordict["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        if index is None:
            expected = f"""TensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([1, 4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([1, 4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        else:
            expected = f"""TensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([4, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""

        assert repr(tensordict) == expected

    @pytest.mark.parametrize("index", [None, (slice(None), 0)])
    def test_repr_indexed_nested_tensordict(self, device, dtype, index):
        nested_tensordict = self.nested_td(device, dtype)[index]
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        tensor_device = device if device else nested_tensordict["b"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        if index is None:
            expected = f"""TensorDict(
    fields={{
        b: {tensor_class}(shape=torch.Size([1, 4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        my_nested_td: TensorDict(
            fields={{
                a: {tensor_class}(shape=torch.Size([1, 4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
            batch_size=torch.Size([1, 4, 3, 2, 1]),
            device={str(device)},
            is_shared={is_shared})}},
    batch_size=torch.Size([1, 4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        else:
            expected = f"""TensorDict(
    fields={{
        b: {tensor_class}(shape=torch.Size([4, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        my_nested_td: TensorDict(
            fields={{
                a: {tensor_class}(shape=torch.Size([4, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
            batch_size=torch.Size([4, 2, 1]),
            device={str(device)},
            is_shared={is_shared})}},
    batch_size=torch.Size([4, 2, 1]),
    device={str(device)},
    is_shared={is_shared})"""
        assert repr(nested_tensordict) == expected

    @pytest.mark.parametrize("index", [None, (slice(None), 0)])
    def test_repr_indexed_stacked_tensordict(self, device, dtype, index):
        stacked_tensordict = self.stacked_td(device, dtype)
        if device is not None and device.type == "cuda":
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        tensor_device = device if device else stacked_tensordict["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared

        expected = f"""LazyStackedTensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    exclusive_fields={{
        0 ->
            c: {tensor_class}(shape=torch.Size([4, 3, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor}),
        1 ->
            b: {tensor_class}(shape=torch.Size([4, 3, 1, 10]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device)},
    is_shared={is_shared},
    stack_dim={stacked_tensordict.stack_dim})"""

        assert repr(stacked_tensordict) == expected

    @pytest.mark.skipif(not torch.cuda.device_count(), reason="no cuda")
    @pytest.mark.parametrize("device_cast", get_available_devices())
    def test_repr_device_to_device(self, device, dtype, device_cast):
        td = self.td(device, dtype)
        if (device_cast is None and (torch.cuda.device_count() > 0)) or (
            device_cast is not None and device_cast.type == "cuda"
        ):
            is_shared = True
        else:
            is_shared = False
        tensor_class = "Tensor"
        td2 = td.to(device_cast)
        tensor_device = device_cast if device_cast else td2["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""TensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2, 1]),
    device={str(device_cast)},
    is_shared={is_shared})"""
        assert repr(td2) == expected

    @pytest.mark.skipif(not torch.cuda.device_count(), reason="no cuda")
    def test_repr_batch_size_update(self, device, dtype):
        td = self.td(device, dtype)
        td.batch_size = torch.Size([4, 3, 2])
        is_shared = False
        tensor_class = "Tensor"
        if device is not None and device.type == "cuda":
            is_shared = True
        tensor_device = device if device else td["a"].device
        if tensor_device.type == "cuda":
            is_shared_tensor = True
        else:
            is_shared_tensor = is_shared
        expected = f"""TensorDict(
    fields={{
        a: {tensor_class}(shape=torch.Size([4, 3, 2, 1, 5]), device={tensor_device}, dtype={dtype}, is_shared={is_shared_tensor})}},
    batch_size=torch.Size([4, 3, 2]),
    device={device},
    is_shared={is_shared})"""
        assert repr(td) == expected


@pytest.mark.parametrize(
    "td_name",
    [
        "td",
        "stacked_td",
        "sub_td",
        "idx_td",
        "unsqueezed_td",
        "td_reset_bs",
    ],
)
@pytest.mark.parametrize(
    "device",
    get_available_devices(),
)
class TestTensorDictsRequiresGrad:
    def td(self, device):
        return TensorDict(
            source={
                "a": torch.randn(3, 1, 5, device=device),
                "b": torch.randn(3, 1, 10, device=device, requires_grad=True),
                "c": torch.randint(10, (3, 1, 3), device=device),
            },
            batch_size=[3, 1],
        )

    def stacked_td(self, device):
        return stack_td([self.td(device) for _ in range(2)], 0)

    def idx_td(self, device):
        return self.td(device)[0]

    def sub_td(self, device):
        return self.td(device).get_sub_tensordict(0)

    def unsqueezed_td(self, device):
        return self.td(device).unsqueeze(0)

    def td_reset_bs(self, device):
        td = self.td(device)
        td = td.unsqueeze(-1).to_tensordict()
        td.batch_size = torch.Size([3, 1])
        return td

    def test_view(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        td_view = td.view(-1)
        assert td_view.get("b").requires_grad

    def test_expand(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        batch_size = td.batch_size
        new_td = td.expand(3, *batch_size)
        assert new_td.get("b").requires_grad
        assert new_td.batch_size == torch.Size([3, *batch_size])

    # Deprecated
    # def test_cast(self, td_name, device):
    #     torch.manual_seed(1)
    #     td = getattr(self, td_name)(device)
    #     td_td = td.to(TensorDict)
    #     assert td_td.get("b").requires_grad

    def test_clone_td(self, td_name, device):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert torch.clone(td).get("b").requires_grad

    def test_squeeze(self, td_name, device, squeeze_dim=-1):
        torch.manual_seed(1)
        td = getattr(self, td_name)(device)
        assert torch.squeeze(td, dim=-1).get("b").requires_grad


def test_batchsize_reset():
    td = TensorDict(
        {"a": torch.randn(3, 4, 5, 6), "b": torch.randn(3, 4, 5)}, batch_size=[3, 4]
    )
    # smoke-test
    td.batch_size = torch.Size([3])

    # test with list
    td.batch_size = [3]

    # test with tuple
    td.batch_size = (3,)

    # incompatible size
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "the tensor a has shape torch.Size([3, 4, 5, "
            "6]) which is incompatible with the new shape torch.Size([3, 5])"
        ),
    ):
        td.batch_size = [3, 5]

    # test set
    td.set("c", torch.randn(3))

    # test index
    td[torch.tensor([1, 2])]
    td[:]
    td[[1, 2]]
    with pytest.raises(
        IndexError,
        match=re.escape("too many indices for tensor of dimension 1"),
    ):
        td[:, 0]

    # test a greater batch_size
    td = TensorDict(
        {"a": torch.randn(3, 4, 5, 6), "b": torch.randn(3, 4, 5)}, batch_size=[3, 4]
    )
    td.batch_size = torch.Size([3, 4, 5])

    td.set("c", torch.randn(3, 4, 5, 6))
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "batch dimension mismatch, "
            "got self.batch_size=torch.Size([3, 4, 5]) and value.shape[:self.batch_dims]=torch.Size([3, 4, 2])"
        ),
    ):
        td.set("d", torch.randn(3, 4, 2))

    # test that lazy tds return an exception
    td_stack = stack_td([TensorDict({"a": torch.randn(3)}, [3]) for _ in range(2)])
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "modifying the batch size of a lazy repesentation "
            "of a tensordict is not permitted. Consider instantiating the tensordict first by calling `td = td.to_tensordict()` before resetting the batch size."
        ),
    ):
        td_stack.batch_size = [2]
    td_stack.to_tensordict().batch_size = [2]

    td = TensorDict({"a": torch.randn(3, 4)}, [3, 4])
    subtd = td.get_sub_tensordict((slice(None), torch.tensor([1, 2])))
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "modifying the batch size of a lazy repesentation of a tensordict is not permitted. Consider instantiating the tensordict first by calling `td = td.to_tensordict()` before resetting the batch size."
        ),
    ):
        subtd.batch_size = [3, 2]
    subtd.to_tensordict().batch_size = [3, 2]

    td = TensorDict({"a": torch.randn(3, 4)}, [3, 4])
    td_u = td.unsqueeze(0)
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "modifying the batch size of a lazy repesentation of a tensordict is not permitted. Consider instantiating the tensordict first by calling `td = td.to_tensordict()` before resetting the batch size."
        ),
    ):
        td_u.batch_size = [1]
    td_u.to_tensordict().batch_size = [1]


@pytest.mark.parametrize("index0", [None, slice(None)])
def test_set_sub_key(index0):
    # tests that parent tensordict is affected when subtensordict is set with a new key
    batch_size = [10, 10]
    source = {"a": torch.randn(10, 10, 10), "b": torch.ones(10, 10, 2)}
    td = TensorDict(source, batch_size=batch_size)
    idx0 = (index0, 0) if index0 is not None else 0
    td0 = td.get_sub_tensordict(idx0)
    idx = (index0, slice(2, 4)) if index0 is not None else slice(2, 4)
    sub_td = td.get_sub_tensordict(idx)
    if index0 is None:
        c = torch.randn(2, 10, 10)
    else:
        c = torch.randn(10, 2, 10)
    sub_td.set("c", c)
    assert (td.get("c")[idx] == sub_td.get("c")).all()
    assert (sub_td.get("c") == c).all()
    assert (td.get("c")[idx0] == 0).all()
    assert (td.get_sub_tensordict(idx0).get("c") == 0).all()
    assert (td0.get("c") == 0).all()


@pytest.mark.skipif(not torch.cuda.device_count(), reason="no cuda")
def test_create_on_device():
    device = torch.device(0)

    # TensorDict
    td = TensorDict({}, [5])
    assert td.device is None

    td.set("a", torch.randn(5, device=device))
    assert td.device is None

    td = TensorDict({}, [5], device="cuda:0")
    td.set("a", torch.randn(5, 1))
    assert td.get("a").device == device

    # stacked TensorDict
    td1 = TensorDict({}, [5])
    td2 = TensorDict({}, [5])
    stackedtd = stack_td([td1, td2], 0)
    assert stackedtd.device is None

    stackedtd.set("a", torch.randn(2, 5, device=device))
    assert stackedtd.device is None

    stackedtd = stackedtd.to(device)
    assert stackedtd.device == device

    td1 = TensorDict({}, [5], device="cuda:0")
    td2 = TensorDict({}, [5], device="cuda:0")
    stackedtd = stack_td([td1, td2], 0)
    stackedtd.set("a", torch.randn(2, 5, 1))
    assert stackedtd.get("a").device == device
    assert td1.get("a").device == device
    assert td2.get("a").device == device

    # TensorDict, indexed
    td = TensorDict({}, [5])
    subtd = td[1]
    assert subtd.device is None

    subtd.set("a", torch.randn(1, device=device))
    # setting element of subtensordict doesn't set top-level device
    assert subtd.device is None

    subtd = subtd.to(device)
    assert subtd.device == device
    assert subtd["a"].device == device

    td = TensorDict({}, [5], device="cuda:0")
    subtd = td[1]
    subtd.set("a", torch.randn(1))
    assert subtd.get("a").device == device

    td = TensorDict({}, [5], device="cuda:0")
    subtd = td[1:3]
    subtd.set("a", torch.randn(2))
    assert subtd.get("a").device == device

    # ViewedTensorDict
    td = TensorDict({}, [6])
    viewedtd = td.view(2, 3)
    assert viewedtd.device is None

    viewedtd = viewedtd.to(device)
    assert viewedtd.device == device

    td = TensorDict({}, [6], device="cuda:0")
    viewedtd = td.view(2, 3)
    a = torch.randn(2, 3)
    viewedtd.set("a", a)
    assert viewedtd.get("a").device == device
    assert (a.to(device) == viewedtd.get("a")).all()


def _remote_process(worker_id, command_pipe_child, command_pipe_parent, tensordict):
    command_pipe_parent.close()
    while True:
        cmd, val = command_pipe_child.recv()
        if cmd == "recv":
            b = tensordict.get("b")
            assert (b == val).all()
            command_pipe_child.send("done")
        elif cmd == "send":
            a = torch.ones(2) * val
            tensordict.set_("a", a)
            assert (
                tensordict.get("a") == a
            ).all(), f'found {a} and {tensordict.get("a")}'
            command_pipe_child.send("done")
        elif cmd == "set_done":
            tensordict.set_("done", torch.ones(1, dtype=torch.bool))
            command_pipe_child.send("done")
        elif cmd == "set_undone_":
            tensordict.set_("done", torch.zeros(1, dtype=torch.bool))
            command_pipe_child.send("done")
        elif cmd == "update":
            tensordict.update_(
                TensorDict(
                    source={"a": tensordict.get("a").clone() + 1},
                    batch_size=tensordict.batch_size,
                )
            )
            command_pipe_child.send("done")
        elif cmd == "update_":
            tensordict.update_(
                TensorDict(
                    source={"a": tensordict.get("a").clone() - 1},
                    batch_size=tensordict.batch_size,
                )
            )
            command_pipe_child.send("done")

        elif cmd == "close":
            command_pipe_child.close()
            break


def _driver_func(tensordict, tensordict_unbind):
    procs = []
    children = []
    parents = []

    for i in range(2):
        command_pipe_parent, command_pipe_child = mp.Pipe()
        proc = mp.Process(
            target=_remote_process,
            args=(i, command_pipe_child, command_pipe_parent, tensordict_unbind[i]),
        )
        proc.start()
        command_pipe_child.close()
        parents.append(command_pipe_parent)
        children.append(command_pipe_child)
        procs.append(proc)

    b = torch.ones(2, 1) * 10
    tensordict.set_("b", b)
    for i in range(2):
        parents[i].send(("recv", 10))
        is_done = parents[i].recv()
        assert is_done == "done"

    for i in range(2):
        parents[i].send(("send", i))
        is_done = parents[i].recv()
        assert is_done == "done"
    a = tensordict.get("a").clone()
    assert (a[0] == 0).all()
    assert (a[1] == 1).all()

    assert not tensordict.get("done").any()
    for i in range(2):
        parents[i].send(("set_done", i))
        is_done = parents[i].recv()
        assert is_done == "done"
    assert tensordict.get("done").all()

    for i in range(2):
        parents[i].send(("set_undone_", i))
        is_done = parents[i].recv()
        assert is_done == "done"
    assert not tensordict.get("done").any()

    a_prev = tensordict.get("a").clone().contiguous()
    for i in range(2):
        parents[i].send(("update_", i))
        is_done = parents[i].recv()
        assert is_done == "done"
    new_a = tensordict.get("a").clone().contiguous()
    torch.testing.assert_close(a_prev - 1, new_a)

    a_prev = tensordict.get("a").clone().contiguous()
    for i in range(2):
        parents[i].send(("update", i))
        is_done = parents[i].recv()
        assert is_done == "done"
    new_a = tensordict.get("a").clone().contiguous()
    torch.testing.assert_close(a_prev + 1, new_a)

    for i in range(2):
        parents[i].send(("close", None))
        procs[i].join()


@pytest.mark.parametrize(
    "td_type",
    [
        "memmap",
        "memmap_stack",
        "contiguous",
        "stack",
    ],
)
def test_mp(td_type):
    tensordict = TensorDict(
        source={
            "a": torch.randn(2, 2),
            "b": torch.randn(2, 1),
            "done": torch.zeros(2, 1, dtype=torch.bool),
        },
        batch_size=[2],
    )
    if td_type == "contiguous":
        tensordict = tensordict.share_memory_()
    elif td_type == "stack":
        tensordict = stack_td(
            [
                tensordict[0].clone().share_memory_(),
                tensordict[1].clone().share_memory_(),
            ],
            0,
        )
    elif td_type == "memmap":
        tensordict = tensordict.memmap_()
    elif td_type == "memmap_stack":
        tensordict = stack_td(
            [
                tensordict[0].clone().memmap_(),
                tensordict[1].clone().memmap_(),
            ],
            0,
        )
    else:
        raise NotImplementedError
    _driver_func(
        tensordict,
        (tensordict.get_sub_tensordict(0), tensordict.get_sub_tensordict(1))
        # tensordict,
        # tensordict.unbind(0),
    )


@pytest.mark.parametrize(
    "idx",
    [
        (slice(None),),
        slice(None),
        (3, 4),
        (3, slice(None), slice(2, 2, 2)),
        (torch.tensor([1, 2, 3]),),
        ([1, 2, 3]),
        (
            torch.tensor([1, 2, 3]),
            torch.tensor([2, 3, 4]),
            torch.tensor([0, 10, 2]),
            torch.tensor([2, 4, 1]),
        ),
        torch.zeros(10, 7, 11, 5, dtype=torch.bool).bernoulli_(),
        torch.zeros(10, 7, 11, dtype=torch.bool).bernoulli_(),
        (0, torch.zeros(7, dtype=torch.bool).bernoulli_()),
    ],
)
def test_getitem_batch_size(idx):
    shape = [10, 7, 11, 5]
    shape = torch.Size(shape)
    mocking_tensor = torch.zeros(*shape)
    expected_shape = mocking_tensor[idx].shape
    resulting_shape = _getitem_batch_size(shape, idx)
    assert expected_shape == resulting_shape, (idx, expected_shape, resulting_shape)


@pytest.mark.parametrize("device", get_available_devices())
def test_requires_grad(device):
    torch.manual_seed(1)
    # Just one of the tensors have requires_grad
    tensordicts = [
        TensorDict(
            batch_size=[11, 12],
            source={
                "key1": torch.randn(
                    11, 12, 5, device=device, requires_grad=True if i == 5 else False
                ),
                "key2": torch.zeros(
                    11, 12, 50, device=device, dtype=torch.bool
                ).bernoulli_(),
            },
        )
        for i in range(10)
    ]
    stacked_td = LazyStackedTensorDict(*tensordicts, stack_dim=0)
    # First stacked tensor has requires_grad == True
    assert list(stacked_td.values())[0].requires_grad is True


@pytest.mark.parametrize("device", get_available_devices())
@pytest.mark.parametrize(
    "td_type", ["tensordict", "view", "unsqueeze", "squeeze", "stack"]
)
@pytest.mark.parametrize("update", [True, False])
def test_filling_empty_tensordict(device, td_type, update):
    if td_type == "tensordict":
        td = TensorDict({}, batch_size=[16], device=device)
    elif td_type == "view":
        td = TensorDict({}, batch_size=[4, 4], device=device).view(-1)
    elif td_type == "unsqueeze":
        td = TensorDict({}, batch_size=[16], device=device).unsqueeze(-1)
    elif td_type == "squeeze":
        td = TensorDict({}, batch_size=[16, 1], device=device).squeeze(-1)
    elif td_type == "stack":
        td = torch.stack([TensorDict({}, [], device=device) for _ in range(16)], 0)
    else:
        raise NotImplementedError

    for i in range(16):
        other_td = TensorDict({"a": torch.randn(10), "b": torch.ones(1)}, [])
        if td_type == "unsqueeze":
            other_td = other_td.unsqueeze(-1).to_tensordict()
        if update:
            subtd = td.get_sub_tensordict(i)
            subtd.update(other_td, inplace=True)
        else:
            td[i] = other_td

    assert td.device == device
    assert td.get("a").device == device
    assert (td.get("b") == 1).all()
    if td_type == "view":
        assert td._source["a"].shape == torch.Size([4, 4, 10])
    elif td_type == "unsqueeze":
        assert td._source["a"].shape == torch.Size([16, 10])
    elif td_type == "squeeze":
        assert td._source["a"].shape == torch.Size([16, 1, 10])
    elif td_type == "stack":
        assert (td[-1] == other_td.to(device)).all()


def test_getitem_nested():
    tensor = torch.randn(4, 5, 6, 7)
    sub_sub_tensordict = TensorDict({"c": tensor}, [4, 5, 6])
    sub_tensordict = TensorDict({}, [4, 5])
    tensordict = TensorDict({}, [4])

    sub_tensordict["b"] = sub_sub_tensordict
    tensordict["a"] = sub_tensordict

    # check that content match
    assert (tensordict["a"] == sub_tensordict).all()
    assert (tensordict["a", "b"] == sub_sub_tensordict).all()
    assert (tensordict["a", "b", "c"] == tensor).all()

    # check that get method returns same contents
    assert (tensordict.get("a") == sub_tensordict).all()
    assert (tensordict.get(("a", "b")) == sub_sub_tensordict).all()
    assert (tensordict.get(("a", "b", "c")) == tensor).all()

    # check that shapes are kept
    assert tensordict.shape == torch.Size([4])
    assert sub_tensordict.shape == torch.Size([4, 5])
    assert sub_sub_tensordict.shape == torch.Size([4, 5, 6])


def test_setitem_nested():
    tensor = torch.randn(4, 5, 6, 7)
    tensor2 = torch.ones(4, 5, 6, 7)
    tensordict = TensorDict({}, [4])
    sub_tensordict = TensorDict({}, [4, 5])
    sub_sub_tensordict = TensorDict({"c": tensor}, [4, 5, 6])
    sub_sub_tensordict2 = TensorDict({"c": tensor2}, [4, 5, 6])
    sub_tensordict["b"] = sub_sub_tensordict
    tensordict["a"] = sub_tensordict
    assert tensordict["a", "b"] is sub_sub_tensordict
    tensordict["a", "b"] = sub_sub_tensordict2
    assert tensordict["a", "b"] is sub_sub_tensordict2
    assert (tensordict["a", "b", "c"] == 1).all()

    # check the same with set method
    sub_tensordict.set("b", sub_sub_tensordict)
    tensordict.set("a", sub_tensordict)
    assert tensordict["a", "b"] is sub_sub_tensordict

    tensordict.set(("a", "b"), sub_sub_tensordict2)
    assert tensordict["a", "b"] is sub_sub_tensordict2
    assert (tensordict["a", "b", "c"] == 1).all()


def test_setdefault_nested():
    tensor = torch.randn(4, 5, 6, 7)
    tensor2 = torch.ones(4, 5, 6, 7)
    sub_sub_tensordict = TensorDict({"c": tensor}, [4, 5, 6])
    sub_tensordict = TensorDict({"b": sub_sub_tensordict}, [4, 5])
    tensordict = TensorDict({"a": sub_tensordict}, [4])

    # if key exists we return the existing value
    assert tensordict.setdefault(("a", "b", "c"), tensor2) is tensor

    assert tensordict.setdefault(("a", "b", "d"), tensor2) is tensor2
    assert (tensordict["a", "b", "d"] == 1).all()
    assert tensordict.get(("a", "b", "d")) is tensor2


@pytest.mark.parametrize("inplace", [True, False])
def test_select_nested(inplace):
    tensor_1 = torch.rand(4, 5, 6, 7)
    tensor_2 = torch.rand(4, 5, 6, 7)
    sub_sub_tensordict = TensorDict(
        {"t1": tensor_1, "t2": tensor_2}, batch_size=[4, 5, 6]
    )
    sub_tensordict = TensorDict(
        {"double_nested": sub_sub_tensordict}, batch_size=[4, 5]
    )
    tensordict = TensorDict(
        {
            "a": torch.rand(4, 3),
            "b": torch.rand(4, 2),
            "c": torch.rand(4, 1),
            "nested": sub_tensordict,
        },
        batch_size=[4],
    )

    selected = tensordict.select(
        "b", ("nested", "double_nested", "t2"), inplace=inplace
    )

    assert set(selected.keys(include_nested=True)) == {
        "b",
        "nested",
        ("nested", "double_nested"),
        ("nested", "double_nested", "t2"),
    }

    if inplace:
        assert selected is tensordict
        assert set(tensordict.keys(include_nested=True)) == {
            "b",
            "nested",
            ("nested", "double_nested"),
            ("nested", "double_nested", "t2"),
        }
    else:
        assert selected is not tensordict
        assert set(tensordict.keys(include_nested=True)) == {
            "a",
            "b",
            "c",
            "nested",
            ("nested", "double_nested"),
            ("nested", "double_nested", "t1"),
            ("nested", "double_nested", "t2"),
        }


def test_select_nested_missing():
    # checks that we keep a nested key even if missing nested keys are present
    td = TensorDict({"a": {"b": [1], "c": [2]}}, [])

    td_select = td.select(("a", "b"), "r", ("a", "z"), strict=False)
    assert ("a", "b") in list(td_select.keys(True, True))
    assert ("a", "b") in td_select.keys(True, True)


@pytest.mark.parametrize("inplace", [True, False])
def test_exclude_nested(inplace):
    tensor_1 = torch.rand(4, 5, 6, 7)
    tensor_2 = torch.rand(4, 5, 6, 7)
    sub_sub_tensordict = TensorDict(
        {"t1": tensor_1, "t2": tensor_2}, batch_size=[4, 5, 6]
    )
    sub_tensordict = TensorDict(
        {"double_nested": sub_sub_tensordict}, batch_size=[4, 5]
    )
    tensordict = TensorDict(
        {
            "a": torch.rand(4, 3),
            "b": torch.rand(4, 2),
            "c": torch.rand(4, 1),
            "nested": sub_tensordict,
        },
        batch_size=[4],
    )
    # making a copy for inplace tests
    tensordict2 = tensordict.clone()

    excluded = tensordict.exclude(
        "b", ("nested", "double_nested", "t2"), inplace=inplace
    )

    assert set(excluded.keys(include_nested=True)) == {
        "a",
        "c",
        "nested",
        ("nested", "double_nested"),
        ("nested", "double_nested", "t1"),
    }

    if inplace:
        assert excluded is tensordict
        assert set(tensordict.keys(include_nested=True)) == {
            "a",
            "c",
            "nested",
            ("nested", "double_nested"),
            ("nested", "double_nested", "t1"),
        }
    else:
        assert excluded is not tensordict
        assert set(tensordict.keys(include_nested=True)) == {
            "a",
            "b",
            "c",
            "nested",
            ("nested", "double_nested"),
            ("nested", "double_nested", "t1"),
            ("nested", "double_nested", "t2"),
        }

    # excluding "nested" should exclude all subkeys also
    excluded2 = tensordict2.exclude("nested", inplace=inplace)
    assert set(excluded2.keys(include_nested=True)) == {"a", "b", "c"}


def test_set_nested_keys():
    tensor = torch.randn(4, 5, 6, 7)
    tensor2 = torch.ones(4, 5, 6, 7)
    tensordict = TensorDict({}, [4])
    sub_tensordict = TensorDict({}, [4, 5])
    sub_sub_tensordict = TensorDict({"c": tensor}, [4, 5, 6])
    sub_sub_tensordict2 = TensorDict({"c": tensor2}, [4, 5, 6])
    sub_tensordict.set("b", sub_sub_tensordict)
    tensordict.set("a", sub_tensordict)
    assert tensordict.get(("a", "b")) is sub_sub_tensordict

    tensordict.set(("a", "b"), sub_sub_tensordict2)
    assert tensordict.get(("a", "b")) is sub_sub_tensordict2
    assert (tensordict.get(("a", "b", "c")) == 1).all()


def test_keys_view():
    tensor = torch.randn(4, 5, 6, 7)
    sub_sub_tensordict = TensorDict({"c": tensor}, [4, 5, 6])
    sub_tensordict = TensorDict({}, [4, 5])
    tensordict = TensorDict({}, [4])

    sub_tensordict["b"] = sub_sub_tensordict
    tensordict["a"] = sub_tensordict

    assert "a" in tensordict.keys()
    assert "random_string" not in tensordict.keys()

    assert ("a",) in tensordict.keys(include_nested=True)
    assert ("a", "b", "c") in tensordict.keys(include_nested=True)
    assert ("a", "c", "b") not in tensordict.keys(include_nested=True)

    with pytest.raises(
        TypeError, match="checks with tuples of strings is only supported"
    ):
        ("a", "b", "c") in tensordict.keys()  # noqa: B015

    with pytest.raises(TypeError, match="TensorDict keys are always strings."):
        42 in tensordict.keys()  # noqa: B015

    with pytest.raises(TypeError, match="TensorDict keys are always strings."):
        ("a", 42) in tensordict.keys()  # noqa: B015

    keys = set(tensordict.keys())
    keys_nested = set(tensordict.keys(include_nested=True))

    assert keys == {"a"}
    assert keys_nested == {"a", ("a", "b"), ("a", "b", "c")}

    leaves = set(tensordict.keys(leaves_only=True))
    leaves_nested = set(tensordict.keys(include_nested=True, leaves_only=True))

    assert leaves == set()
    assert leaves_nested == {("a", "b", "c")}


def test_error_on_contains():
    td = TensorDict(
        {"a": TensorDict({"b": torch.rand(1, 2)}, [1, 2]), "c": torch.rand(1)}, [1]
    )
    with pytest.raises(
        NotImplementedError,
        match="TensorDict does not support membership checks with the `in` keyword",
    ):
        "random_string" in td  # noqa: B015


@pytest.mark.parametrize("method", ["share_memory", "memmap"])
def test_memory_lock(method):
    torch.manual_seed(1)
    td = TensorDict({"a": torch.randn(4, 5)}, batch_size=(4, 5))

    # lock=True
    if method == "share_memory":
        td.share_memory_()
    elif method == "memmap":
        td.memmap_()
    else:
        raise NotImplementedError

    td.set("a", torch.randn(4, 5), inplace=True)
    td.set_("a", torch.randn(4, 5))  # No exception because set_ ignores the lock

    with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
        td.set("a", torch.randn(4, 5))

    with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
        td.set("b", torch.randn(4, 5))

    with pytest.raises(RuntimeError, match="Cannot modify locked TensorDict"):
        td.set("b", torch.randn(4, 5), inplace=True)


class TestMakeTensorDict:
    def test_create_tensordict(self):
        tensordict = make_tensordict(a=torch.zeros(3, 4))
        assert (tensordict["a"] == torch.zeros(3, 4)).all()

    def test_tensordict_batch_size(self):
        tensordict = make_tensordict()
        assert tensordict.batch_size == torch.Size([])

        tensordict = make_tensordict(a=torch.randn(3, 4))
        assert tensordict.batch_size == torch.Size([3, 4])

        tensordict = make_tensordict(a=torch.randn(3, 4), b=torch.randn(3, 4, 5))
        assert tensordict.batch_size == torch.Size([3, 4])

        nested_tensordict = make_tensordict(c=tensordict, d=torch.randn(3, 5))  # nested
        assert nested_tensordict.batch_size == torch.Size([3])

        nested_tensordict = make_tensordict(c=tensordict, d=torch.randn(4, 5))  # nested
        assert nested_tensordict.batch_size == torch.Size([])

        tensordict = make_tensordict(a=torch.randn(3, 4, 2), b=torch.randn(3, 4, 5))
        assert tensordict.batch_size == torch.Size([3, 4])

        tensordict = make_tensordict(a=torch.randn(3, 4), b=torch.randn(1))
        assert tensordict.batch_size == torch.Size([])

        tensordict = make_tensordict(
            a=torch.randn(3, 4), b=torch.randn(3, 4, 5), batch_size=[3]
        )
        assert tensordict.batch_size == torch.Size([3])

        tensordict = make_tensordict(
            a=torch.randn(3, 4), b=torch.randn(3, 4, 5), batch_size=[]
        )
        assert tensordict.batch_size == torch.Size([])

    @pytest.mark.parametrize("device", get_available_devices())
    def test_tensordict_device(self, device):
        tensordict = make_tensordict(
            a=torch.randn(3, 4), b=torch.randn(3, 4), device=device
        )
        assert tensordict.device == device
        assert tensordict["a"].device == device
        assert tensordict["b"].device == device

        tensordict = make_tensordict(
            a=torch.randn(3, 4, device=device),
            b=torch.randn(3, 4),
            c=torch.randn(3, 4, device="cpu"),
            device=device,
        )
        assert tensordict.device == device
        assert tensordict["a"].device == device
        assert tensordict["b"].device == device
        assert tensordict["c"].device == device

    def test_nested(self):
        input_dict = {
            "a": {"b": torch.randn(3, 4), "c": torch.randn(3, 4, 5)},
            "d": torch.randn(3),
        }
        tensordict = make_tensordict(input_dict)
        assert tensordict.shape == torch.Size([3])
        assert tensordict["a"].shape == torch.Size([3, 4])
        input_tensordict = TensorDict(
            {
                "a": {"b": torch.randn(3, 4), "c": torch.randn(3, 4, 5)},
                "d": torch.randn(3),
            },
            [],
        )
        tensordict = make_tensordict(input_tensordict)
        assert tensordict.shape == torch.Size([3])
        assert tensordict["a"].shape == torch.Size([3, 4])
        input_dict = {
            ("a", "b"): torch.randn(3, 4),
            ("a", "c"): torch.randn(3, 4, 5),
            "d": torch.randn(3),
        }
        tensordict = make_tensordict(input_dict)
        assert tensordict.shape == torch.Size([3])
        assert tensordict["a"].shape == torch.Size([3, 4])


def test_update_nested_dict():
    t = TensorDict({"a": {"d": [[[0]] * 3] * 2}}, [2, 3])
    assert ("a", "d") in t.keys(include_nested=True)
    t.update({"a": {"b": [[[1]] * 3] * 2}})
    assert ("a", "d") in t.keys(include_nested=True)
    assert ("a", "b") in t.keys(include_nested=True)
    assert t["a", "b"].shape == torch.Size([2, 3, 1])
    t.update({"a": {"d": [[[1]] * 3] * 2}})


@pytest.mark.parametrize("inplace", [True, False])
@pytest.mark.parametrize("separator", [",", "-"])
def test_flatten_unflatten_key_collision(inplace, separator):
    td1 = TensorDict(
        {
            f"a{separator}b{separator}c": torch.zeros(3),
            "a": {"b": {"c": torch.zeros(3)}},
        },
        [],
    )
    td2 = TensorDict(
        {
            f"a{separator}b": torch.zeros(3),
            "a": {"b": torch.zeros(3)},
            "g": {"d": torch.zeros(3)},
        },
        [],
    )
    td3 = TensorDict(
        {
            f"a{separator}b{separator}c": torch.zeros(3),
            "a": {"b": {"c": torch.zeros(3), "d": torch.zeros(3)}},
        },
        [],
    )

    td4 = TensorDict(
        {
            f"a{separator}b{separator}c{separator}d": torch.zeros(3),
            "a": {"b": {"c": torch.zeros(3)}},
        },
        [],
    )

    td5 = TensorDict(
        {f"a{separator}b": torch.zeros(3), "a": {"b": {"c": torch.zeros(3)}}}, []
    )

    with pytest.raises(
        KeyError, match="Flattening keys in tensordict collides with existing key *"
    ):
        _ = td1.flatten_keys(separator)

    with pytest.raises(
        KeyError, match="Flattening keys in tensordict collides with existing key *"
    ):
        _ = td2.flatten_keys(separator)

    with pytest.raises(
        KeyError, match="Flattening keys in tensordict collides with existing key *"
    ):
        _ = td3.flatten_keys(separator)

    with pytest.raises(
        KeyError,
        match=re.escape(
            "Unflattening key(s) in tensordict will override existing unflattened key"
        ),
    ):
        _ = td1.unflatten_keys(separator)

    with pytest.raises(
        KeyError,
        match=re.escape(
            "Unflattening key(s) in tensordict will override existing unflattened key"
        ),
    ):
        _ = td2.unflatten_keys(separator)

    with pytest.raises(
        KeyError,
        match=re.escape(
            "Unflattening key(s) in tensordict will override existing unflattened key"
        ),
    ):
        _ = td3.unflatten_keys(separator)

    with pytest.raises(
        KeyError,
        match=re.escape(
            "Unflattening key(s) in tensordict will override existing unflattened key"
        ),
    ):
        _ = td4.unflatten_keys(separator)

    with pytest.raises(
        KeyError,
        match=re.escape(
            "Unflattening key(s) in tensordict will override existing unflattened key"
        ),
    ):
        _ = td5.unflatten_keys(separator)

    td4_flat = td4.flatten_keys(separator)
    assert (f"a{separator}b{separator}c{separator}d") in td4_flat.keys()
    assert (f"a{separator}b{separator}c") in td4_flat.keys()

    td5_flat = td5.flatten_keys(separator)
    assert (f"a{separator}b") in td5_flat.keys()
    assert (f"a{separator}b{separator}c") in td5_flat.keys()


def test_split_with_invalid_arguments():
    td = TensorDict({"a": torch.zeros(2, 1)}, [])
    # Test empty batch size
    with pytest.raises(RuntimeError, match="not splittable"):
        td.split(1, 0)

    td = TensorDict({}, [3, 2])

    # Test invalid split_size input
    with pytest.raises(TypeError, match="must be int or list of ints"):
        td.split("1", 0)
    with pytest.raises(TypeError, match="must be int or list of ints"):
        td.split(["1", 2], 0)

    # Test invalid split_size sum
    with pytest.raises(RuntimeError, match="expects split_size to sum exactly"):
        td.split([], 0)

    with pytest.raises(RuntimeError, match="expects split_size to sum exactly"):
        td.split([1, 1], 0)

    # Test invalid dimension input
    with pytest.raises(IndexError, match="Dimension out of range"):
        td.split(1, 2)
    with pytest.raises(IndexError, match="Dimension out of range"):
        td.split(1, -3)


def test_split_with_empty_tensordict():
    td = TensorDict({}, [10])

    tds = td.split(4, 0)
    assert len(tds) == 3
    assert tds[0].shape == torch.Size([4])
    assert tds[1].shape == torch.Size([4])
    assert tds[2].shape == torch.Size([2])

    tds = td.split([1, 9], 0)

    assert len(tds) == 2
    assert tds[0].shape == torch.Size([1])
    assert tds[1].shape == torch.Size([9])

    td = TensorDict({}, [10, 10, 3])

    tds = td.split(4, 1)
    assert len(tds) == 3
    assert tds[0].shape == torch.Size([10, 4, 3])
    assert tds[1].shape == torch.Size([10, 4, 3])
    assert tds[2].shape == torch.Size([10, 2, 3])

    tds = td.split([1, 9], 1)
    assert len(tds) == 2
    assert tds[0].shape == torch.Size([10, 1, 3])
    assert tds[1].shape == torch.Size([10, 9, 3])


def test_split_with_negative_dim():
    td = TensorDict({"a": torch.zeros(5, 4, 2, 1), "b": torch.zeros(5, 4, 1)}, [5, 4])

    tds = td.split([1, 3], -1)
    assert len(tds) == 2
    assert tds[0].shape == torch.Size([5, 1])
    assert tds[0]["a"].shape == torch.Size([5, 1, 2, 1])
    assert tds[0]["b"].shape == torch.Size([5, 1, 1])
    assert tds[1].shape == torch.Size([5, 3])
    assert tds[1]["a"].shape == torch.Size([5, 3, 2, 1])
    assert tds[1]["b"].shape == torch.Size([5, 3, 1])


def test_shared_inheritance():
    td = TensorDict({"a": torch.randn(3, 4)}, [3, 4])
    td.share_memory_()

    td0, *_ = td.unbind(1)
    assert td0.is_shared()

    td0, *_ = td.split(1, 0)
    assert td0.is_shared()

    td0 = td.exclude("a")
    assert td0.is_shared()

    td0 = td.select("a")
    assert td0.is_shared()

    td.unlock_()
    td0 = td.rename_key_("a", "a.a")
    assert not td0.is_shared()
    td.share_memory_()

    td0 = td.unflatten_keys(".")
    assert td0.is_shared()

    td0 = td.flatten_keys(".")
    assert td0.is_shared()

    td0 = td.view(-1)
    assert td0.is_shared()

    td0 = td.permute(1, 0)
    assert td0.is_shared()

    td0 = td.unsqueeze(0)
    assert td0.is_shared()

    td0 = td0.squeeze(0)
    assert td0.is_shared()


class TestLazyStackedTensorDict:
    @staticmethod
    def nested_lazy_het_td(batch_size):
        shared = torch.zeros(4, 4, 2)
        hetero_3d = torch.zeros(3)
        hetero_2d = torch.zeros(2)

        individual_0_tensor = torch.zeros(1)
        individual_1_tensor = torch.zeros(1, 2)
        individual_2_tensor = torch.zeros(1, 2, 3)

        td_list = [
            TensorDict(
                {
                    "shared": shared,
                    "hetero": hetero_3d,
                    "individual_0_tensor": individual_0_tensor,
                },
                [],
            ),
            TensorDict(
                {
                    "shared": shared,
                    "hetero": hetero_3d,
                    "individual_1_tensor": individual_1_tensor,
                },
                [],
            ),
            TensorDict(
                {
                    "shared": shared,
                    "hetero": hetero_2d,
                    "individual_2_tensor": individual_2_tensor,
                },
                [],
            ),
        ]
        for i, td in enumerate(td_list):
            td[f"individual_{i}_td"] = td.clone()
            td["shared_td"] = td.clone()

        td_stack = torch.stack(td_list, dim=0)
        obs = TensorDict(
            {"lazy": td_stack, "dense": torch.zeros(3, 3, 2)},
            [],
        )
        obs = obs.expand(batch_size).clone()
        return obs

    @pytest.mark.parametrize("batch_size", [(), (2,), (1, 2)])
    @pytest.mark.parametrize("cat_dim", [0, 1, 2])
    def test_cat_lazy_stack(self, batch_size, cat_dim):
        if cat_dim > len(batch_size):
            return
        td_lazy = self.nested_lazy_het_td(batch_size)["lazy"]

        res = torch.cat([td_lazy], dim=cat_dim)
        assert assert_allclose_td(res, td_lazy)
        assert res is not td_lazy
        td_lazy_clone = td_lazy.clone()
        data_ptr_set_before = {val.data_ptr() for val in decompose(td_lazy)}
        res = torch.cat([td_lazy_clone], dim=cat_dim, out=td_lazy)
        data_ptr_set_after = {val.data_ptr() for val in decompose(td_lazy)}
        assert data_ptr_set_after == data_ptr_set_before
        assert res is td_lazy
        assert assert_allclose_td(res, td_lazy_clone)

        td_lazy_2 = td_lazy.clone()
        td_lazy_2.apply_(lambda x: x + 1)

        res = torch.cat([td_lazy, td_lazy_2], dim=cat_dim)
        assert res.stack_dim == len(batch_size)
        assert res.shape[cat_dim] == td_lazy.shape[cat_dim] + td_lazy_2.shape[cat_dim]
        index = (slice(None),) * cat_dim + (slice(0, td_lazy.shape[cat_dim]),)
        assert assert_allclose_td(res[index], td_lazy)
        index = (slice(None),) * cat_dim + (slice(td_lazy.shape[cat_dim], None),)
        assert assert_allclose_td(res[index], td_lazy_2)

        res = torch.cat([td_lazy, td_lazy_2], dim=cat_dim)
        assert res.stack_dim == len(batch_size)
        assert res.shape[cat_dim] == td_lazy.shape[cat_dim] + td_lazy_2.shape[cat_dim]
        index = (slice(None),) * cat_dim + (slice(0, td_lazy.shape[cat_dim]),)
        assert assert_allclose_td(res[index], td_lazy)
        index = (slice(None),) * cat_dim + (slice(td_lazy.shape[cat_dim], None),)
        assert assert_allclose_td(res[index], td_lazy_2)

        if cat_dim != len(batch_size):  # cat dim is not stack dim
            batch_size = list(batch_size)
            batch_size[cat_dim] *= 2
            td_lazy_dest = self.nested_lazy_het_td(batch_size)["lazy"]
            data_ptr_set_before = {val.data_ptr() for val in decompose(td_lazy_dest)}
            res = torch.cat([td_lazy, td_lazy_2], dim=cat_dim, out=td_lazy_dest)
            data_ptr_set_after = {val.data_ptr() for val in decompose(td_lazy_dest)}
            assert data_ptr_set_after == data_ptr_set_before
            assert res is td_lazy_dest
            index = (slice(None),) * cat_dim + (slice(0, td_lazy.shape[cat_dim]),)
            assert assert_allclose_td(res[index], td_lazy)
            index = (slice(None),) * cat_dim + (slice(td_lazy.shape[cat_dim], None),)
            assert assert_allclose_td(res[index], td_lazy_2)

    def recursively_check_key(self, td, value: int):
        if isinstance(td, LazyStackedTensorDict):
            for t in td.tensordicts:
                if not self.recursively_check_key(t, value):
                    return False
        elif isinstance(td, TensorDict):
            for i in td.values():
                if not self.recursively_check_key(i, value):
                    return False
        elif isinstance(td, torch.Tensor):
            return (td == value).all()
        else:
            return False

        return True

    def dense_stack_tds_v1(self, td_list, stack_dim: int) -> TensorDictBase:
        shape = list(td_list[0].shape)
        shape.insert(stack_dim, len(td_list))

        out = td_list[0].unsqueeze(stack_dim).expand(shape).clone()
        for i in range(1, len(td_list)):
            index = (slice(None),) * stack_dim + (i,)  # this is index_select
            out[index] = td_list[i]

        return out

    def dense_stack_tds_v2(self, td_list, stack_dim: int) -> TensorDictBase:
        shape = list(td_list[0].shape)
        shape.insert(stack_dim, len(td_list))
        out = td_list[0].unsqueeze(stack_dim).expand(shape).clone()

        data_ptr_set_before = {val.data_ptr() for val in decompose(out)}
        res = torch.stack(td_list, dim=stack_dim, out=out)
        data_ptr_set_after = {val.data_ptr() for val in decompose(out)}
        assert data_ptr_set_before == data_ptr_set_after

        return res

    @pytest.mark.parametrize("batch_size", [(), (2,), (1, 2)])
    @pytest.mark.parametrize("stack_dim", [0, 1, 2])
    def test_setitem_hetero(self, batch_size, stack_dim):
        obs = self.nested_lazy_het_td(batch_size)
        obs1 = obs.clone()
        obs1.apply_(lambda x: x + 1)

        if stack_dim > len(batch_size):
            return

        res1 = self.dense_stack_tds_v1([obs, obs1], stack_dim=stack_dim)
        res2 = self.dense_stack_tds_v2([obs, obs1], stack_dim=stack_dim)

        index = (slice(None),) * stack_dim + (0,)  # get the first in the stack
        assert self.recursively_check_key(res1[index], 0)  # check all 0
        assert self.recursively_check_key(res2[index], 0)  # check all 0
        index = (slice(None),) * stack_dim + (1,)  # get the second in the stack
        assert self.recursively_check_key(res1[index], 1)  # check all 1
        assert self.recursively_check_key(res2[index], 1)  # check all 1

    @pytest.mark.parametrize("batch_size", [(), (32,), (32, 4)])
    def test_lazy_stack_stack(self, batch_size):
        obs = self.nested_lazy_het_td(batch_size)

        assert isinstance(obs, TensorDict)
        assert isinstance(obs["lazy"], LazyStackedTensorDict)
        assert obs["lazy"].stack_dim == len(obs["lazy"].shape) - 1  # succeeds
        assert obs["lazy"].shape == (*batch_size, 3)
        assert isinstance(obs["lazy"][..., 0], TensorDict)  # succeeds

        obs_stack = torch.stack([obs])

        assert (
            isinstance(obs_stack, LazyStackedTensorDict) and obs_stack.stack_dim == 0
        )  # succeeds
        assert obs_stack.batch_size == (1, *batch_size)  # succeeds
        assert obs_stack[0] is obs  # succeeds
        assert isinstance(obs_stack["lazy"], LazyStackedTensorDict)
        assert obs_stack["lazy"].shape == (1, *batch_size, 3)
        assert obs_stack["lazy"].stack_dim == 0  # succeeds
        assert obs_stack["lazy"][0] is obs["lazy"]

        obs2 = obs.clone()
        obs_stack = torch.stack([obs, obs2])

        assert (
            isinstance(obs_stack, LazyStackedTensorDict) and obs_stack.stack_dim == 0
        )  # succeeds
        assert obs_stack.batch_size == (2, *batch_size)  # succeeds
        assert obs_stack[0] is obs  # succeeds
        assert isinstance(obs_stack["lazy"], LazyStackedTensorDict)
        assert obs_stack["lazy"].shape == (2, *batch_size, 3)
        assert obs_stack["lazy"].stack_dim == 0  # succeeds
        assert obs_stack["lazy"][0] is obs["lazy"]

    @pytest.mark.parametrize("batch_size", [(), (32,), (32, 4)])
    def test_stack_hetero(self, batch_size):
        obs = self.nested_lazy_het_td(batch_size)

        obs2 = obs.clone()
        obs2.apply_(lambda x: x + 1)

        obs_stack = torch.stack([obs, obs2])
        obs_stack_resolved = self.dense_stack_tds_v2([obs, obs2], stack_dim=0)

        assert isinstance(obs_stack, LazyStackedTensorDict) and obs_stack.stack_dim == 0
        assert isinstance(obs_stack_resolved, TensorDict)

        assert obs_stack.batch_size == (2, *batch_size)
        assert obs_stack_resolved.batch_size == obs_stack.batch_size

        assert obs_stack["lazy"].shape == (2, *batch_size, 3)
        assert obs_stack_resolved["lazy"].batch_size == obs_stack["lazy"].batch_size

        assert obs_stack["lazy"].stack_dim == 0
        assert (
            obs_stack_resolved["lazy"].stack_dim
            == len(obs_stack_resolved["lazy"].batch_size) - 1
        )
        for stack in [obs_stack_resolved, obs_stack]:
            for index in range(2):
                assert (stack[index]["dense"] == index).all()
                assert (stack["dense"][index] == index).all()
                assert (stack["lazy"][index]["shared"] == index).all()
                assert (stack[index]["lazy"]["shared"] == index).all()
                assert (stack["lazy"]["shared"][index] == index).all()
                assert (
                    stack["lazy"][index][..., 0]["individual_0_tensor"] == index
                ).all()
                assert (
                    stack[index]["lazy"][..., 0]["individual_0_tensor"] == index
                ).all()
                assert (
                    stack["lazy"][..., 0]["individual_0_tensor"][index] == index
                ).all()
                assert (
                    stack["lazy"][..., 0][index]["individual_0_tensor"] == index
                ).all()

    def test_add_batch_dim_cache(self):
        td = TensorDict(
            {"a": torch.rand(3, 4, 5), ("b", "c"): torch.rand(3, 4, 5)}, [3, 4, 5]
        )
        td = torch.stack([td, td.clone()], 0)
        from tensordict.nn import TensorDictModule  # noqa
        from torch import vmap

        print("first call to vmap")
        fun = vmap(lambda x: x)
        fun(td)
        td.zero_()
        # this value should be cached
        print("second call to vmap")
        std = fun(td)
        for value in std.values(True, True):
            assert (value == 0).all()

    def test_add_batch_dim_cache_nested(self):
        td = TensorDict(
            {"a": torch.rand(3, 4, 5), ("b", "c"): torch.rand(3, 4, 5)}, [3, 4, 5]
        )
        td = TensorDict({"parent": torch.stack([td, td.clone()], 0)}, [2, 3, 4, 5])
        from tensordict.nn import TensorDictModule  # noqa
        from torch import vmap

        fun = vmap(lambda x: x)
        print("first call to vmap")
        fun(td)
        td.zero_()
        # this value should be cached
        print("second call to vmap")
        std = fun(td)
        for value in std.values(True, True):
            assert (value == 0).all()

    def test_update_with_lazy(self):
        td0 = TensorDict(
            {
                ("a", "b", "c"): torch.ones(3, 4),
                ("a", "b", "d"): torch.ones(3, 4),
                "common": torch.ones(3),
            },
            [3],
        )
        td1 = TensorDict(
            {
                ("a", "b", "c"): torch.ones(3, 5) * 2,
                "common": torch.ones(3) * 2,
            },
            [3],
        )
        td = TensorDict({"parent": torch.stack([td0, td1], 0)}, [2])

        td_void = TensorDict(
            {
                ("parent", "a", "b", "c"): torch.zeros(2, 3, 4),
                ("parent", "a", "b", "e"): torch.zeros(2, 3, 4),
                ("parent", "a", "b", "d"): torch.zeros(2, 3, 5),
            },
            [2],
        )
        td_void.update(td)
        assert type(td_void.get("parent")) is LazyStackedTensorDict
        assert type(td_void.get(("parent", "a"))) is LazyStackedTensorDict
        assert type(td_void.get(("parent", "a", "b"))) is LazyStackedTensorDict
        assert (td_void.get(("parent", "a", "b"))[0].get("c") == 1).all()
        assert (td_void.get(("parent", "a", "b"))[1].get("c") == 2).all()
        assert (td_void.get(("parent", "a", "b"))[0].get("d") == 1).all()
        assert (td_void.get(("parent", "a", "b"))[1].get("d") == 0).all()  # unaffected
        assert (td_void.get(("parent", "a", "b")).get("e") == 0).all()  # unaffected

    @pytest.mark.parametrize("unsqueeze_dim", [0, 1, -1, -2])
    def test_stack_unsqueeze(self, unsqueeze_dim):
        td = TensorDict({("a", "b"): torch.ones(3, 4, 5)}, [3, 4])
        td_stack = torch.stack(td.unbind(1), 1)
        td_unsqueeze = td.unsqueeze(unsqueeze_dim)
        td_stack_unsqueeze = td_stack.unsqueeze(unsqueeze_dim)
        assert isinstance(td_stack_unsqueeze, LazyStackedTensorDict)
        for key in td_unsqueeze.keys(True, True):
            assert td_unsqueeze.get(key).shape == td_stack_unsqueeze.get(key).shape

    def test_stack_apply(self):
        td0 = TensorDict(
            {
                ("a", "b", "c"): torch.ones(3, 4),
                ("a", "b", "d"): torch.ones(3, 4),
                "common": torch.ones(3),
            },
            [3],
        )
        td1 = TensorDict(
            {
                ("a", "b", "c"): torch.ones(3, 5) * 2,
                "common": torch.ones(3) * 2,
            },
            [3],
        )
        td = TensorDict({"parent": torch.stack([td0, td1], 0)}, [2])
        td2 = td.clone()
        tdapply = td.apply(lambda x, y: x + y, td2)
        assert isinstance(tdapply["parent", "a", "b"], LazyStackedTensorDict)
        assert (tdapply["parent", "a", "b"][0]["c"] == 2).all()
        assert (tdapply["parent", "a", "b"][1]["c"] == 4).all()
        assert (tdapply["parent", "a", "b"][0]["d"] == 2).all()

    def test_stack_keys(self):
        td1 = TensorDict(source={"a": torch.randn(3)}, batch_size=[])
        td2 = TensorDict(
            source={
                "a": torch.randn(3),
                "b": torch.randn(3),
                "c": torch.randn(4),
                "d": torch.randn(5),
            },
            batch_size=[],
        )
        td = stack_td([td1, td2], 0)
        assert "a" in td.keys()
        assert "b" not in td.keys()
        assert "b" in td[1].keys()
        td.set("b", torch.randn(2, 10), inplace=False)  # overwrites
        with pytest.raises(KeyError):
            td.set_("c", torch.randn(2, 10))  # overwrites
        td.set_("b", torch.randn(2, 10))  # b has been set before

        td1.set("c", torch.randn(4))
        td[
            "c"
        ]  # we must first query that key for the stacked tensordict to update the list
        assert "c" in td.keys(), list(td.keys())  # now all tds have the key c
        td.get("c")

        td1.set("d", torch.randn(6))
        with pytest.raises(RuntimeError):
            td.get("d")

        td["e"] = torch.randn(2, 4)
        assert "e" in td.keys()  # now all tds have the key c
        td.get("e")

    def test_stacked_td_nested_keys(self):
        td = torch.stack(
            [
                TensorDict({"a": {"b": {"d": [1]}, "c": [2]}}, []),
                TensorDict({"a": {"b": {"d": [1]}, "d": [2]}}, []),
            ],
            0,
        )
        assert ("a", "b") in td.keys(True)
        assert ("a", "c") not in td.keys(True)
        assert ("a", "b", "d") in td.keys(True)
        td["a", "c"] = [[2], [3]]
        assert ("a", "c") in td.keys(True)

        keys, items = zip(*td.items(True))
        assert ("a", "b") in keys
        assert ("a", "c") in keys
        assert ("a", "d") not in keys

        td["a", "c"] = td["a", "c"] + 1
        assert (td["a", "c"] == torch.tensor([[3], [4]], device=td.device)).all()

    @pytest.mark.parametrize("device", get_available_devices())
    @pytest.mark.parametrize("stack_dim", [0, 1])
    def test_stacked_td(self, stack_dim, device):
        tensordicts = [
            TensorDict(
                batch_size=[11, 12],
                source={
                    "key1": torch.randn(11, 12, 5, device=device),
                    "key2": torch.zeros(
                        11, 12, 50, device=device, dtype=torch.bool
                    ).bernoulli_(),
                },
            )
            for _ in range(10)
        ]

        tensordicts0 = tensordicts[0]
        tensordicts1 = tensordicts[1]
        tensordicts2 = tensordicts[2]
        tensordicts3 = tensordicts[3]
        sub_td = LazyStackedTensorDict(*tensordicts, stack_dim=stack_dim)

        std_bis = stack_td(tensordicts, dim=stack_dim, contiguous=False)
        assert (sub_td == std_bis).all()

        item = (*[slice(None) for _ in range(stack_dim)], 0)
        tensordicts0.zero_()
        assert (sub_td[item].get("key1") == sub_td.get("key1")[item]).all()
        assert (
            sub_td.contiguous()[item].get("key1")
            == sub_td.contiguous().get("key1")[item]
        ).all()
        assert (sub_td.contiguous().get("key1")[item] == 0).all()

        item = (*[slice(None) for _ in range(stack_dim)], 1)
        std2 = sub_td[:5]
        tensordicts1.zero_()
        assert (std2[item].get("key1") == std2.get("key1")[item]).all()
        assert (
            std2.contiguous()[item].get("key1") == std2.contiguous().get("key1")[item]
        ).all()
        assert (std2.contiguous().get("key1")[item] == 0).all()

        std3 = sub_td[:5, :, :5]
        tensordicts2.zero_()
        item = (*[slice(None) for _ in range(stack_dim)], 2)
        assert (std3[item].get("key1") == std3.get("key1")[item]).all()
        assert (
            std3.contiguous()[item].get("key1") == std3.contiguous().get("key1")[item]
        ).all()
        assert (std3.contiguous().get("key1")[item] == 0).all()

        std4 = sub_td.select("key1")
        tensordicts3.zero_()
        item = (*[slice(None) for _ in range(stack_dim)], 3)
        assert (std4[item].get("key1") == std4.get("key1")[item]).all()
        assert (
            std4.contiguous()[item].get("key1") == std4.contiguous().get("key1")[item]
        ).all()
        assert (std4.contiguous().get("key1")[item] == 0).all()

        std5 = sub_td.unbind(1)[0]
        assert (std5.contiguous() == sub_td.contiguous().unbind(1)[0]).all()

    @pytest.mark.parametrize("device", get_available_devices())
    @pytest.mark.parametrize("stack_dim", [0, 1, 2])
    def test_stacked_indexing(self, device, stack_dim):
        tensordict = TensorDict(
            {"a": torch.randn(3, 4, 5), "b": torch.randn(3, 4, 5)},
            batch_size=[3, 4, 5],
            device=device,
        )

        tds = torch.stack(list(tensordict.unbind(stack_dim)), stack_dim)

        for item, expected_shape in (
            ((2, 2), torch.Size([5])),
            ((slice(1, 2), 2), torch.Size([1, 5])),
            ((..., 2), torch.Size([3, 4])),
        ):
            assert tds[item].batch_size == expected_shape
            assert (tds[item].get("a") == tds.get("a")[item]).all()
            assert (tds[item].get("a") == tensordict[item].get("a")).all()

    @pytest.mark.parametrize("device", get_available_devices())
    def test_stack(self, device):
        torch.manual_seed(1)
        tds_list = [TensorDict(source={}, batch_size=(4, 5)) for _ in range(3)]
        tds = stack_td(tds_list, 0, contiguous=False)
        assert tds[0] is tds_list[0]

        td = TensorDict(
            source={"a": torch.randn(4, 5, 3, device=device)}, batch_size=(4, 5)
        )
        td_list = list(td)
        td_reconstruct = stack_td(td_list, 0)
        assert td_reconstruct.batch_size == td.batch_size
        assert (td_reconstruct == td).all()

    @pytest.mark.parametrize("dim", range(2))
    @pytest.mark.parametrize("index", range(2))
    @pytest.mark.parametrize("device", get_available_devices())
    def test_lazy_stacked_insert(self, dim, index, device):
        td = TensorDict({"a": torch.zeros(4)}, [4], device=device)
        lstd = torch.stack([td] * 2, dim=dim)

        lstd.insert(
            index,
            TensorDict(
                {"a": torch.ones(4), "invalid": torch.rand(4)}, [4], device=device
            ),
        )

        bs = [4]
        bs.insert(dim, 3)

        assert lstd.batch_size == torch.Size(bs)
        assert set(lstd.keys()) == {"a"}

        t = torch.zeros(*bs, device=device)

        if dim == 0:
            t[index] = 1
        else:
            t[:, index] = 1

        torch.testing.assert_close(lstd["a"], t)

        with pytest.raises(
            TypeError, match="Expected new value to be TensorDictBase instance"
        ):
            lstd.insert(index, torch.rand(10))

        if device != torch.device("cpu"):
            with pytest.raises(ValueError, match="Devices differ"):
                lstd.insert(index, TensorDict({"a": torch.ones(4)}, [4], device="cpu"))

        with pytest.raises(ValueError, match="Batch sizes in tensordicts differs"):
            lstd.insert(index, TensorDict({"a": torch.ones(17)}, [17], device=device))

    def test_lazy_stacked_contains(self):
        td = TensorDict(
            {"a": TensorDict({"b": torch.rand(1, 2)}, [1, 2]), "c": torch.rand(1)}, [1]
        )
        lstd = torch.stack([td, td, td])

        assert td in lstd
        assert td.clone() not in lstd

        with pytest.raises(
            NotImplementedError,
            match="TensorDict does not support membership checks with the `in` keyword",
        ):
            "random_string" in lstd  # noqa: B015

    @pytest.mark.parametrize("dim", range(2))
    @pytest.mark.parametrize("device", get_available_devices())
    def test_lazy_stacked_append(self, dim, device):
        td = TensorDict({"a": torch.zeros(4)}, [4], device=device)
        lstd = torch.stack([td] * 2, dim=dim)

        lstd.append(
            TensorDict(
                {"a": torch.ones(4), "invalid": torch.rand(4)}, [4], device=device
            )
        )

        bs = [4]
        bs.insert(dim, 3)

        assert lstd.batch_size == torch.Size(bs)
        assert set(lstd.keys()) == {"a"}

        t = torch.zeros(*bs, device=device)

        if dim == 0:
            t[-1] = 1
        else:
            t[:, -1] = 1

        torch.testing.assert_close(lstd["a"], t)

        with pytest.raises(
            TypeError, match="Expected new value to be TensorDictBase instance"
        ):
            lstd.append(torch.rand(10))

        if device != torch.device("cpu"):
            with pytest.raises(ValueError, match="Devices differ"):
                lstd.append(TensorDict({"a": torch.ones(4)}, [4], device="cpu"))

        with pytest.raises(ValueError, match="Batch sizes in tensordicts differs"):
            lstd.append(TensorDict({"a": torch.ones(17)}, [17], device=device))

    def test_unbind_lazystack(self):
        td0 = TensorDict(
            {
                "a": {"b": torch.randn(3, 4), "d": torch.randn(3, 4)},
                "c": torch.randn(3, 4),
            },
            [3, 4],
        )
        td = torch.stack([td0, td0, td0], 1)

        assert all(_td is td0 for _td in td.unbind(1))

    @pytest.mark.parametrize("stack_dim", [0, 1, -1])
    def test_stack_update_heter_stacked_td(self, stack_dim):
        td1 = TensorDict({"a": torch.randn(3, 4)}, [3])
        td2 = TensorDict({"a": torch.randn(3, 5)}, [3])
        td_a = torch.stack([td1, td2], stack_dim)
        td_b = td_a.clone()
        td_a.update(td_b)
        with pytest.raises(
            RuntimeError,
            match="Found more than one unique shape in the tensors to be stacked",
        ):
            td_a.update(td_b.to_tensordict())
        td_a.update_(td_b)
        with pytest.raises(
            RuntimeError,
            match="Found more than one unique shape in the tensors to be stacked",
        ):
            td_a.update_(td_b.to_tensordict())

    @property
    def _tensor_index(self):
        torch.manual_seed(0)
        return torch.randint(2, (5, 2))

    @property
    def _idx_list(self):
        return {
            0: 1,
            1: slice(None),
            2: slice(1, 2),
            3: self._tensor_index,
            4: range(1, 2),
            5: None,
            6: [0, 1],
            7: self._tensor_index.numpy(),
        }

    @pytest.mark.parametrize("pos1", range(8))
    @pytest.mark.parametrize("pos2", range(8))
    @pytest.mark.parametrize("pos3", range(8))
    def test_lazy_indexing(self, pos1, pos2, pos3):
        torch.manual_seed(0)
        td_leaf_1 = TensorDict({"a": torch.ones(2, 3)}, [])
        inner = torch.stack([td_leaf_1] * 4, 0)
        middle = torch.stack([inner] * 3, 0)
        outer = torch.stack([middle] * 2, 0)
        outer_dense = outer.to_tensordict()
        ref_tensor = torch.zeros(2, 3, 4)
        pos1 = self._idx_list[pos1]
        pos2 = self._idx_list[pos2]
        pos3 = self._idx_list[pos3]
        index = (pos1, pos2, pos3)
        result = outer[index]
        assert result.batch_size == ref_tensor[index].shape, index
        assert result.batch_size == outer_dense[index].shape, index

    @pytest.mark.parametrize("stack_dim", [0, 1, 2])
    @pytest.mark.parametrize("mask_dim", [0, 1, 2])
    @pytest.mark.parametrize("single_mask_dim", [True, False])
    @pytest.mark.parametrize("device", get_available_devices())
    def test_lazy_mask_indexing(self, stack_dim, mask_dim, single_mask_dim, device):
        torch.manual_seed(0)
        td = TensorDict({"a": torch.zeros(9, 10, 11)}, [9, 10, 11], device=device)
        td = torch.stack(
            [
                td,
                td.apply(lambda x: x + 1),
                td.apply(lambda x: x + 2),
                td.apply(lambda x: x + 3),
            ],
            stack_dim,
        )
        mask = torch.zeros(())
        while not mask.any():
            if single_mask_dim:
                mask = torch.zeros(td.shape[mask_dim], dtype=torch.bool).bernoulli_()
            else:
                mask = torch.zeros(
                    td.shape[mask_dim : mask_dim + 2], dtype=torch.bool
                ).bernoulli_()
        index = (slice(None),) * mask_dim + (mask,)
        tdmask = td[index]
        assert tdmask["a"].shape == td["a"][index].shape
        assert (tdmask["a"] == td["a"][index]).all()
        index = (0,) * mask_dim + (mask,)
        tdmask = td[index]
        assert tdmask["a"].shape == td["a"][index].shape
        assert (tdmask["a"] == td["a"][index]).all()
        index = (slice(1),) * mask_dim + (mask,)
        tdmask = td[index]
        assert tdmask["a"].shape == td["a"][index].shape
        assert (tdmask["a"] == td["a"][index]).all()

    @pytest.mark.parametrize("stack_dim", [0, 1, 2])
    @pytest.mark.parametrize("mask_dim", [0, 1, 2])
    @pytest.mark.parametrize("single_mask_dim", [True, False])
    @pytest.mark.parametrize("device", get_available_devices())
    def test_lazy_mask_setitem(self, stack_dim, mask_dim, single_mask_dim, device):
        torch.manual_seed(0)
        td = TensorDict({"a": torch.zeros(9, 10, 11)}, [9, 10, 11], device=device)
        td = torch.stack(
            [
                td,
                td.apply(lambda x: x + 1),
                td.apply(lambda x: x + 2),
                td.apply(lambda x: x + 3),
            ],
            stack_dim,
        )
        mask = torch.zeros(())
        while not mask.any():
            if single_mask_dim:
                mask = torch.zeros(td.shape[mask_dim], dtype=torch.bool).bernoulli_()
            else:
                mask = torch.zeros(
                    td.shape[mask_dim : mask_dim + 2], dtype=torch.bool
                ).bernoulli_()
        index = (slice(None),) * mask_dim + (mask,)
        tdset = TensorDict({"a": td["a"][index] * 0 - 1}, [])
        # we know that the batch size is accurate from test_lazy_mask_indexing
        tdset.batch_size = td[index].batch_size
        td[index] = tdset
        assert (td["a"][index] == tdset["a"]).all()
        assert (td["a"][index] == tdset["a"]).all()
        index = (slice(1),) * mask_dim + (mask,)
        tdset = TensorDict({"a": td["a"][index] * 0 - 1}, [])
        tdset.batch_size = td[index].batch_size
        td[index] = tdset
        assert (td["a"][index] == tdset["a"]).all()
        assert (td["a"][index] == tdset["a"]).all()

    def test_all_keys(self):
        td = TensorDict({"a": torch.zeros(1)}, [])
        td2 = TensorDict({"a": torch.zeros(2)}, [])
        stack = torch.stack([td, td2])
        assert set(stack.keys(True, True)) == {"a"}


@pytest.mark.skipif(
    not _has_torchsnapshot, reason=f"torchsnapshot not found: err={TORCHSNAPSHOT_ERR}"
)
class TestSnapshot:
    @pytest.mark.parametrize("save_name", ["doc", "data"])
    def test_inplace(self, save_name):
        td = TensorDict(
            {"a": torch.randn(3), "b": TensorDict({"c": torch.randn(3, 1)}, [3, 1])},
            [3],
        )
        td.memmap_()
        assert isinstance(td["b", "c"], MemmapTensor)

        app_state = {
            "state": torchsnapshot.StateDict(
                **{save_name: td.state_dict(keep_vars=True)}
            )
        }
        path = f"/tmp/{uuid.uuid4()}"
        snapshot = torchsnapshot.Snapshot.take(app_state=app_state, path=path)

        td_plain = td.to_tensordict()
        # we want to delete refs to MemmapTensors
        assert not isinstance(td_plain["a"], MemmapTensor)
        del td

        snapshot = torchsnapshot.Snapshot(path=path)
        td_dest = TensorDict(
            {"a": torch.zeros(3), "b": TensorDict({"c": torch.zeros(3, 1)}, [3, 1])},
            [3],
        )
        td_dest.memmap_()
        assert isinstance(td_dest["b", "c"], MemmapTensor)
        app_state = {
            "state": torchsnapshot.StateDict(
                **{save_name: td_dest.state_dict(keep_vars=True)}
            )
        }
        snapshot.restore(app_state=app_state)

        assert (td_dest == td_plain).all()
        assert td_dest["b"].batch_size == td_plain["b"].batch_size
        assert isinstance(td_dest["b", "c"], MemmapTensor)

    def test_update(
        self,
    ):
        tensordict = TensorDict({"a": torch.randn(3), "b": {"c": torch.randn(3)}}, [])
        state = {"state": tensordict}
        tensordict.memmap_()
        path = f"/tmp/{uuid.uuid4()}"
        snapshot = torchsnapshot.Snapshot.take(app_state=state, path=path)
        td_plain = tensordict.to_tensordict()
        assert not isinstance(td_plain["a"], MemmapTensor)
        del tensordict

        snapshot = torchsnapshot.Snapshot(path=path)
        tensordict2 = TensorDict({}, [])
        target_state = {"state": tensordict2}
        snapshot.restore(app_state=target_state)
        assert (td_plain == tensordict2).all()


@pytest.mark.parametrize("device", get_available_devices())
def test_memmap_as_tensor(device):
    td = TensorDict(
        {"a": torch.randn(3, 4), "b": {"c": torch.randn(3, 4)}}, [3, 4], device="cpu"
    )
    td_memmap = td.clone().memmap_()
    assert (td == td_memmap).all()

    assert (td == td_memmap.apply(lambda x: x.as_tensor())).all()
    if device.type == "cuda":
        td = td.pin_memory()
        td_memmap = td.clone().memmap_()
        td_memmap_pm = td_memmap.apply(lambda x: x.as_tensor()).pin_memory()
        assert (td.pin_memory().to(device) == td_memmap_pm.to(device)).all()


def test_tensordict_prealloc_nested():
    N = 3
    B = 5
    T = 4
    buffer = TensorDict({}, batch_size=[B, N])

    td_0 = TensorDict(
        {
            "env.time": torch.rand(N, 1),
            "agent.obs": TensorDict(
                {  # assuming 3 agents in a multi-agent setting
                    "image": torch.rand(N, T, 64),
                    "state": torch.rand(N, T, 3, 32, 32),
                },
                batch_size=[N, T],
            ),
        },
        batch_size=[N],
    )

    td_1 = td_0.clone()
    buffer[0] = td_0
    buffer[1] = td_1
    assert (
        repr(buffer)
        == """TensorDict(
    fields={
        agent.obs: TensorDict(
            fields={
                image: Tensor(shape=torch.Size([5, 3, 4, 64]), device=cpu, dtype=torch.float32, is_shared=False),
                state: Tensor(shape=torch.Size([5, 3, 4, 3, 32, 32]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([5, 3, 4]),
            device=None,
            is_shared=False),
        env.time: Tensor(shape=torch.Size([5, 3, 1]), device=cpu, dtype=torch.float32, is_shared=False)},
    batch_size=torch.Size([5, 3]),
    device=None,
    is_shared=False)"""
    )
    assert buffer.batch_size == torch.Size([B, N])
    assert buffer["agent.obs"].batch_size == torch.Size([B, N, T])


@pytest.mark.parametrize("like", [True, False])
def test_save_load_memmap_stacked_td(
    like,
    tmpdir,
):
    a = TensorDict({"a": [1]}, [])
    b = TensorDict({"b": [1]}, [])
    c = torch.stack([a, b])
    c = c.expand(10, 2)
    if like:
        d = c.memmap_like(prefix=tmpdir)
    else:
        d = c.memmap_(prefix=tmpdir)

    d2 = LazyStackedTensorDict.load_memmap(tmpdir)
    assert (d2 == d).all()
    assert (d2[:, 0] == d[:, 0]).all()
    if like:
        assert (d2[:, 0] == a.zero_()).all()
    else:
        assert (d2[:, 0] == a).all()


class TestErrorMessage:
    @staticmethod
    def test_err_msg_missing_nested():
        td = TensorDict({"a": torch.zeros(())}, [])
        with pytest.raises(ValueError, match="Expected a TensorDictBase instance"):
            td["a", "b"]

    @staticmethod
    def test_inplace_error():
        td = TensorDict({"a": torch.rand(())}, [])
        with pytest.raises(ValueError, match="Failed to update 'a'"):
            td.set_("a", torch.randn(2))


@pytest.mark.parametrize("batch_first", [True, False])
@pytest.mark.parametrize("make_mask", [True, False])
def test_pad_sequence(batch_first, make_mask):
    list_td = [
        TensorDict({"a": torch.ones((2,)), ("b", "c"): torch.ones((2, 3))}, [2]),
        TensorDict({"a": torch.ones((4,)), ("b", "c"): torch.ones((4, 3))}, [4]),
    ]
    padded_td = pad_sequence(list_td, batch_first=batch_first, return_mask=make_mask)
    if batch_first:
        assert padded_td.shape == torch.Size([2, 4])
        assert padded_td["a"].shape == torch.Size([2, 4])
        assert padded_td["a"][0, -1] == 0
        assert padded_td["b", "c"].shape == torch.Size([2, 4, 3])
        assert padded_td["b", "c"][0, -1, 0] == 0
    else:
        assert padded_td.shape == torch.Size([4, 2])
        assert padded_td["a"].shape == torch.Size([4, 2])
        assert padded_td["a"][-1, 0] == 0
        assert padded_td["b", "c"].shape == torch.Size([4, 2, 3])
        assert padded_td["b", "c"][-1, 0, 0] == 0
    if make_mask:
        assert "mask" in padded_td.keys()
        assert not padded_td["mask"].all()
    else:
        assert "mask" not in padded_td.keys()


class TestNamedDims(TestTensorDictsBase):
    def test_noname(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        assert td.names == [None] * 4

    def test_fullname(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=["a", "b", "c", "d"])
        assert td.names == ["a", "b", "c", "d"]

    def test_partial_name(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=["a", None, None, "d"])
        assert td.names == ["a", None, None, "d"]

    def test_partial_set(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        td.names = ["a", None, None, "d"]
        assert td.names == ["a", None, None, "d"]
        td.names = ["a", "b", "c", "d"]
        assert td.names == ["a", "b", "c", "d"]
        with pytest.raises(
            ValueError,
            match="the length of the dimension names must equate the tensordict batch_dims",
        ):
            td.names = ["a", "b", "c"]

    def test_rename(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        td.names = ["a", None, None, "d"]
        td.rename_(a="c")
        assert td.names == ["c", None, None, "d"]
        td.rename_(d="z")
        assert td.names == ["c", None, None, "z"]
        td.rename_(*list("mnop"))
        assert td.names == ["m", "n", "o", "p"]
        td2 = td.rename(p="q")
        assert td.names == ["m", "n", "o", "p"]
        assert td2.names == ["m", "n", "o", "q"]
        td2 = td.rename(*list("wxyz"))
        assert td.names == ["m", "n", "o", "p"]
        assert td2.names == ["w", "x", "y", "z"]

    def test_stack(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=["a", "b", "c", "d"])
        tds = torch.stack([td, td], 0)
        assert tds.names == [None, "a", "b", "c", "d"]
        tds = torch.stack([td, td], -1)
        assert tds.names == ["a", "b", "c", "d", None]
        tds = torch.stack([td, td], 2)
        tds.names = list("mnopq")
        assert tds.names == list("mnopq")
        assert td.names == ["m", "n", "p", "q"]

    def test_cat(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        tdc = torch.cat([td, td], -1)
        assert tdc.names == [None] * 4
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=["a", "b", "c", "d"])
        tdc = torch.cat([td, td], -1)
        assert tdc.names == ["a", "b", "c", "d"]

    def test_unsqueeze(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        td.names = ["a", "b", "c", "d"]
        tdu = td.unsqueeze(0)
        assert tdu.names == [None, "a", "b", "c", "d"]
        tdu = td.unsqueeze(-1)
        assert tdu.names == ["a", "b", "c", "d", None]
        tdu = td.unsqueeze(2)
        assert tdu.names == ["a", "b", None, "c", "d"]

    def test_squeeze(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        td.names = ["a", "b", "c", "d"]
        tds = td.squeeze(0)
        assert tds.names == ["a", "b", "c", "d"]
        td = TensorDict({}, batch_size=[3, 1, 5, 6], names=None)
        td.names = ["a", "b", "c", "d"]
        tds = td.squeeze(1)
        assert tds.names == ["a", "c", "d"]

    def test_clone(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        td.names = ["a", "b", "c", "d"]
        tdc = td.clone()
        assert tdc.names == ["a", "b", "c", "d"]
        tdc = td.clone(False)
        assert tdc.names == ["a", "b", "c", "d"]

    def test_permute(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=None)
        td.names = ["a", "b", "c", "d"]
        tdp = td.permute(-1, -2, -3, -4)
        assert tdp.names == list("dcba")
        tdp = td.permute(-1, 1, 2, -4)
        assert tdp.names == list("dbca")

    def test_refine_names(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6])
        tdr = td.refine_names(None, None, None, "d")
        assert tdr.names == [None, None, None, "d"]
        tdr = tdr.refine_names(None, None, "c", "d")
        assert tdr.names == [None, None, "c", "d"]
        with pytest.raises(
            RuntimeError, match="refine_names: cannot coerce TensorDict"
        ):
            tdr.refine_names(None, None, "d", "d")
        tdr = td.refine_names(..., "d")
        assert tdr.names == [None, None, "c", "d"]
        tdr = td.refine_names("a", ..., "d")
        assert tdr.names == ["a", None, "c", "d"]

    def test_index(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=["a", "b", "c", "d"])
        assert td[0].names == ["b", "c", "d"]
        assert td[:, 0].names == ["a", "c", "d"]
        assert td[0, :].names == ["b", "c", "d"]
        assert td[0, :1].names == ["b", "c", "d"]
        assert td[..., -1].names == ["a", "b", "c"]
        assert td[0, ..., -1].names == ["b", "c"]
        assert td[0, ..., [-1]].names == ["b", "c", "d"]
        assert td[0, ..., torch.tensor([-1])].names == ["b", "c", "d"]
        assert td[0, ..., torch.tensor(-1)].names == ["b", "c"]
        assert td[0, ..., :-1].names == ["b", "c", "d"]
        assert td[:1, ..., :-1].names == ["a", "b", "c", "d"]
        tdbool = td[torch.ones(3, dtype=torch.bool)]
        assert tdbool.names == [None, "b", "c", "d"]
        assert tdbool.ndim == 4
        tdbool = td[torch.ones(3, 4, dtype=torch.bool)]
        assert tdbool.names == [None, "c", "d"]
        assert tdbool.ndim == 3

    def test_subtd(self):
        td = TensorDict({}, batch_size=[3, 4, 5, 6], names=["a", "b", "c", "d"])
        assert td.get_sub_tensordict(0).names == ["b", "c", "d"]
        assert td.get_sub_tensordict((slice(None), 0)).names == ["a", "c", "d"]
        assert td.get_sub_tensordict((0, slice(None))).names == ["b", "c", "d"]
        assert td.get_sub_tensordict((0, slice(None, 1))).names == ["b", "c", "d"]
        assert td.get_sub_tensordict((..., -1)).names == ["a", "b", "c"]
        assert td.get_sub_tensordict((0, ..., -1)).names == ["b", "c"]
        assert td.get_sub_tensordict((0, ..., [-1])).names == ["b", "c", "d"]
        assert td.get_sub_tensordict((0, ..., torch.tensor([-1]))).names == [
            "b",
            "c",
            "d",
        ]
        assert td.get_sub_tensordict((0, ..., torch.tensor(-1))).names == ["b", "c"]
        assert td.get_sub_tensordict((0, ..., slice(None, -1))).names == ["b", "c", "d"]
        assert td.get_sub_tensordict((slice(None, 1), ..., slice(None, -1))).names == [
            "a",
            "b",
            "c",
            "d",
        ]
        tdbool = td.get_sub_tensordict(torch.ones(3, dtype=torch.bool))
        assert tdbool.names == [None, "b", "c", "d"]
        assert tdbool.ndim == 4
        tdbool = td.get_sub_tensordict(torch.ones(3, 4, dtype=torch.bool))
        assert tdbool.names == [None, "c", "d"]
        assert tdbool.ndim == 3
        with pytest.raises(
            RuntimeError, match="Names of a subtensordict cannot be modified"
        ):
            tdbool.names = "All work and no play makes Jack a dull boy"

    def test_expand(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        tde = td.expand(2, 3, 4, 5, 6)
        assert tde.names == [None, "a", "b", "c", "d"]

    def test_apply(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        tda = td.apply(lambda x: x + 1)
        assert tda.names == ["a", "b", "c", "d"]
        tda = td.apply(lambda x: x.squeeze(2), batch_size=[3, 4, 6])
        # no way to tell what the names have become, in general
        assert tda.names == [None] * 3

    def test_flatten(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        tdf = td.flatten(1, 3)
        assert tdf.names == ["a", None]
        tdu = tdf.unflatten(1, (4, 1, 6))
        assert tdu.names == ["a", None, None, None]
        tdf = td.flatten(1, 2)
        assert tdf.names == ["a", None, "d"]
        tdu = tdf.unflatten(1, (4, 1))
        assert tdu.names == ["a", None, None, "d"]
        tdf = td.flatten(0, 2)
        assert tdf.names == [None, "d"]
        tdu = tdf.unflatten(0, (3, 4, 1))
        assert tdu.names == [None, None, None, "d"]

    def test_gather(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        idx = torch.randint(6, (3, 4, 1, 18))
        tdg = td.gather(dim=-1, index=idx)
        assert tdg.names == ["a", "b", "c", "d"]

    def test_select(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        tds = td.select()
        assert tds.names == ["a", "b", "c", "d"]
        tde = td.exclude()
        assert tde.names == ["a", "b", "c", "d"]
        td[""] = torch.zeros(td.shape)
        td["*"] = torch.zeros(td.shape)
        tds = td.select("")
        assert tds.names == ["a", "b", "c", "d"]

    def test_detach(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        td[""] = torch.zeros(td.shape, requires_grad=True)
        tdd = td.detach()
        assert tdd.names == ["a", "b", "c", "d"]

    def test_unbind(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        *_, tdu = td.unbind(-1)
        assert tdu.names == ["a", "b", "c"]
        *_, tdu = td.unbind(-2)
        assert tdu.names == ["a", "b", "d"]

    def test_split(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        _, tdu = td.split(dim=-1, split_size=[3, 3])
        assert tdu.names == ["a", "b", "c", "d"]
        _, tdu = td.split(dim=1, split_size=[1, 3])
        assert tdu.names == ["a", "b", "c", "d"]

    def test_all(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        tda = td.all(2)
        assert tda.names == ["a", "b", "d"]
        tda = td.any(2)
        assert tda.names == ["a", "b", "d"]

    def test_masked_fill(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        tdm = td.masked_fill(torch.zeros(3, 4, 1, dtype=torch.bool), 1.0)
        assert tdm.names == ["a", "b", "c", "d"]

    def test_memmap_like(self, tmpdir):
        td = TensorDict(
            {"a": torch.zeros(3, 4, 1, 6)},
            batch_size=[3, 4, 1, 6],
            names=["a", "b", "c", "d"],
        )
        tdm = td.memmap_like(prefix=tmpdir)
        assert tdm.names == ["a", "b", "c", "d"]

    def test_h5(self, tmpdir):
        td = TensorDict(
            {"a": torch.zeros(3, 4, 1, 6)},
            batch_size=[3, 4, 1, 6],
            names=["a", "b", "c", "d"],
        )
        tdm = td.to_h5(filename=tmpdir / "file.h5")
        assert tdm.names == ["a", "b", "c", "d"]

    def test_nested(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "d"])
        td["a"] = TensorDict({}, batch_size=[3, 4, 1, 6])
        assert td["a"].names == td.names
        td["a"] = TensorDict({}, batch_size=[])
        assert td["a"].names == td.names
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=None)
        td["a"] = TensorDict({}, batch_size=[3, 4, 1, 6])
        td.names = ["a", "b", None, None]
        assert td["a"].names == td.names
        td.set_("a", TensorDict({}, batch_size=[3, 4, 1, 6]))
        assert td["a"].names == td.names

    def test_error_similar(self):
        with pytest.raises(ValueError):
            td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "a"])
        with pytest.raises(ValueError):
            td = TensorDict(
                {},
                batch_size=[3, 4, 1, 6],
            )
            td.names = ["a", "b", "c", "a"]
        with pytest.raises(ValueError):
            td = TensorDict(
                {},
                batch_size=[3, 4, 1, 6],
            )
            td.refine_names("a", "a", ...)
        with pytest.raises(ValueError):
            td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "z"])
            td.rename_(a="z")

    def test_set_at(self):
        td = TensorDict(
            {"": TensorDict({}, [3, 4, 1, 6])},
            batch_size=[3, 4, 1, 6],
            names=["a", "b", "c", "d"],
        )
        td.set_at_("", TensorDict({}, [4, 1, 6]), 0)
        assert td.names == ["a", "b", "c", "d"]
        assert td[""].names == ["a", "b", "c", "d"]

    def test_change_batch_size(self):
        td = TensorDict({}, batch_size=[3, 4, 1, 6], names=["a", "b", "c", "z"])
        td.batch_size = [3, 4, 1, 6, 1]
        assert td.names == ["a", "b", "c", "z", None]
        td.batch_size = []
        assert td.names == []
        td.batch_size = [3, 4]
        assert td.names == [None, None]
        td.names = ["a", None]
        td.batch_size = [3]
        assert td.names == ["a"]

    def test_set_item_populate_names(self):
        td = TensorDict({}, [3])
        td["a"] = TensorDict({}, [3, 4], names=["a", "b"])
        assert td.names == ["a"]
        assert td["a"].names == ["a", "b"]

    def test_nested_indexing(self):
        td = TensorDict(
            {"": TensorDict({}, [3, 4], names=["c", "d"])}, [3], names=["c"]
        )
        assert td[0][""].names == td[""][0].names == ["d"]

    @pytest.mark.parametrize("device", get_available_devices())
    def test_to(self, device):
        td = TensorDict(
            {"": TensorDict({}, [3, 4, 1, 6])},
            batch_size=[3, 4, 1, 6],
            names=["a", "b", "c", "d"],
        )
        tdt = td.to(device)
        assert tdt.names == ["a", "b", "c", "d"]

    def test_stack_assign(self):
        td = TensorDict(
            {"": TensorDict({}, [3, 4], names=["c", "d"])}, [3], names=["c"]
        )
        tds = torch.stack([td, td], -1)
        assert tds.names == ["c", None]
        assert tds[""].names == ["c", None, "d"]
        with pytest.raises(ValueError):
            tds.names = ["c", "d"]
        tds.names = ["c", "e"]
        assert tds.names == ["c", "e"]
        assert tds[""].names == ["c", "e", "d"]
        assert tds[0].names == ["e"]
        assert tds[0][""].names == tds[""][0].names == ["e", "d"]

    def test_nested_td(self):
        nested_td = self.nested_td("cpu")
        nested_td.names = list("abcd")
        assert nested_td.rename(c="g").names == list("abgd")
        assert nested_td.names == list("abcd")
        nested_td.rename_(c="g")
        assert nested_td.names == list("abgd")
        assert nested_td["my_nested_td"].names == list("abgd")
        assert nested_td.contiguous().names == list("abgd")
        assert nested_td.contiguous()["my_nested_td"].names == list("abgd")

    def test_nested_tc(self):
        nested_td = self.nested_tensorclass("cpu")
        nested_td.names = list("abcd")
        assert nested_td.rename(c="g").names == list("abgd")
        assert nested_td.names == list("abcd")
        nested_td.rename_(c="g")
        assert nested_td.names == list("abgd")
        assert nested_td.get("my_nested_tc").names == list("abgd")
        assert nested_td.contiguous().names == list("abgd")
        assert nested_td.contiguous().get("my_nested_tc").names == list("abgd")

    def test_nested_stacked_td(self):
        td = self.nested_stacked_td("cpu")
        td.names = list("abcd")
        assert td.names == list("abcd")
        assert td[:, 1].names == list("acd")
        assert td["my_nested_td"][:, 1].names == list("acd")
        assert td[:, 1]["my_nested_td"].names == list("acd")
        tdr = td.rename(c="z")
        assert td.names == list("abcd")
        assert tdr.names == list("abzd")
        td.rename_(c="z")
        assert td.names == list("abzd")
        assert td[:, 1].names == list("azd")
        assert td["my_nested_td"][:, 1].names == list("azd")
        assert td[:, 1]["my_nested_td"].names == list("azd")
        assert td.contiguous().names == list("abzd")
        assert td[:, 1].contiguous()["my_nested_td"].names == list("azd")

    def test_sub_td(self):
        td = self.sub_td("cpu")
        with pytest.raises(
            RuntimeError, match="Names of a subtensordict cannot be modified"
        ):
            td.names = list("abcd")
        td = self.sub_td2("cpu")
        with pytest.raises(
            RuntimeError, match="Names of a subtensordict cannot be modified"
        ):
            td.names = list("abcd")

    def test_memmap_td(self):
        td = self.memmap_td("cpu")
        td.names = list("abcd")
        assert td.rename(c="g").names == list("abgd")
        assert td.names == list("abcd")
        td.rename_(c="g")
        assert td.names == list("abgd")
        assert td.as_tensor().names == list("abgd")

    def test_h5_td(self):
        td = self.td_h5("cpu")
        td.names = list("abcd")
        assert td.rename(c="g").names == list("abgd")
        assert td.names == list("abcd")
        td.rename_(c="g")
        assert td.names == list("abgd")

    def test_permute_td(self):
        td = self.unsqueezed_td("cpu")
        with pytest.raises(
            RuntimeError, match="Names of a lazy tensordict cannot be modified"
        ):
            td.names = list("abcd")

    def test_squeeze_td(self):
        td = self.squeezed_td("cpu")
        with pytest.raises(
            RuntimeError, match="Names of a lazy tensordict cannot be modified"
        ):
            td.names = list("abcd")

    def test_unsqueeze_td(self):
        td = self.unsqueezed_td("cpu")
        with pytest.raises(
            RuntimeError, match="Names of a lazy tensordict cannot be modified"
        ):
            td.names = list("abcd")


def _compare_tensors_identity(td0, td1):
    if isinstance(td0, LazyStackedTensorDict):
        if not isinstance(td1, LazyStackedTensorDict):
            return False
        for _td0, _td1 in zip(td0.tensordicts, td1.tensordicts):
            if not _compare_tensors_identity(_td0, _td1):
                return False
        return True
    if td0 is td1:
        return True
    for key, val in td0.items():
        if is_tensor_collection(val):
            if not _compare_tensors_identity(val, td1.get(key)):
                return False
        else:
            if val is not td1.get(key):
                return False
    else:
        return True


class TestLock:
    def test_nested_lock(self):
        td = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td = td.lock_()
        td._lock_id, id(td)
        a = td["a"]
        b = td["a", "b"]
        c = td["a", "b", "c"]
        assert len(a._lock_id) == 1
        assert len(b._lock_id) == 2
        assert len(c._lock_id) == 3
        td = td.unlock_()
        assert len(a._lock_id) == 0, a._lock_id
        assert len(b._lock_id) == 0, b._lock_id
        assert len(c._lock_id) == 0, c._lock_id
        td = td.lock_()
        del td
        assert len(a._lock_id) == 0
        assert len(b._lock_id) == 1
        assert len(c._lock_id) == 2
        a = a.lock_()
        del a
        assert len(b._lock_id) == 0
        assert len(c._lock_id) == 1
        b = b.lock_()
        del b
        assert len(c._lock_id) == 0

    def test_nested_lock_erros(self):
        td = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td = td.lock_()
        a = td["a"]
        b = td["a", "b"]
        c = td["a", "b", "c"]
        # we cannot unlock a
        with pytest.raises(
            RuntimeError,
            match="Cannot unlock a tensordict that is part of a locked graph.",
        ):
            a.unlock_()
        with pytest.raises(
            RuntimeError,
            match="Cannot unlock a tensordict that is part of a locked graph.",
        ):
            b.unlock_()
        assert len(a._lock_id) == 1
        assert len(b._lock_id) == 2
        assert len(c._lock_id) == 3
        del td
        a.unlock_()
        a.lock_()
        with pytest.raises(
            RuntimeError,
            match="Cannot unlock a tensordict that is part of a locked graph.",
        ):
            b.unlock_()

    def test_lock_two_roots(self):
        td = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td = td.lock_()
        td._lock_id, id(td)
        a = td["a"]
        b = td["a", "b"]
        c = td["a", "b", "c"]
        other_td = TensorDict({"a": a}, [])
        other_td.lock_()
        # we cannot unlock anything anymore
        with pytest.raises(
            RuntimeError,
            match="Cannot unlock a tensordict that is part of a locked graph.",
        ):
            other_td.unlock_()
        with pytest.raises(
            RuntimeError,
            match="Cannot unlock a tensordict that is part of a locked graph.",
        ):
            td.unlock_()
        # if we group them we can't unlock
        supertd = TensorDict({"td": td, "other": other_td}, [])
        supertd = supertd.lock_()
        supertd = supertd.unlock_()
        supertd = supertd.lock_()
        idsuper = id(supertd)
        idother = id(other_td)
        del supertd, other_td
        assert len(td._lock_id) == 0
        assert idsuper not in a._lock_id
        assert idother not in a._lock_id
        assert len(a._lock_id) == 1
        assert idsuper not in b._lock_id
        assert idother not in b._lock_id
        assert len(b._lock_id) == 2
        assert len(c._lock_id) == 3
        td.unlock_()

    def test_lock_stack(self):
        td0 = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td1 = td0.clone()
        td = torch.stack([td0, td1])
        td = td.lock_()
        a = td["a"]
        b = td["a", "b"]
        c = td["a", "b", "c"]
        a0 = td0["a"]
        b0 = td0["a", "b"]
        c0 = td0["a", "b", "c"]
        assert len(a._lock_id) == 3  # td, td0, td1
        assert len(b._lock_id) == 5  # td, td0, td1, a0, a1
        assert len(c._lock_id) == 7  # td, td0, td1, a0, a1, b0, b1
        assert len(a0._lock_id) == 2  # td, td0
        assert len(b0._lock_id) == 3  # td, td0, a0
        assert len(c0._lock_id) == 4  # td, td0, a0, b0
        td.unlock_()
        td.lock_()
        del td, td0, td1
        a.unlock_()
        a.lock_()
        assert len(a._lock_id) == 0
        assert len(b._lock_id) == 3  # a, a0, a1
        assert len(c._lock_id) == 5  # a, a0, a1, b0, b1
        assert len(a0._lock_id) == 1  # a
        assert len(b0._lock_id) == 2  # a, a0
        assert len(c0._lock_id) == 3  # a, a0, b0
        del a, a0
        b.unlock_()
        b.lock_()
        del b

    def test_empty_tensordict_list(self):
        td = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        a = td["a"]
        b = td["a", "b"]
        c = td["a", "b", "c"]
        td = td.lock_()
        assert len(td._locked_tensordicts)
        assert len(a._locked_tensordicts)
        assert len(b._locked_tensordicts)
        td.unlock_()
        assert not len(td._locked_tensordicts)
        assert not len(a._locked_tensordicts)
        assert not len(b._locked_tensordicts)
        assert not len(c._locked_tensordicts)

    def test_empty_tensordict_list_stack(self):
        td0 = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td1 = td0.clone()
        td = torch.stack([td0, td1])
        td = td.lock_()
        a = td["a"]
        b = td["a", "b"]
        c = td["a", "b", "c"]
        a0 = td0["a"]
        b0 = td0["a", "b"]
        c0 = td0["a", "b", "c"]
        assert not hasattr(td, "_locked_tensordicts")
        assert not hasattr(a, "_locked_tensordicts")
        assert not hasattr(b, "_locked_tensordicts")
        assert not hasattr(c, "_locked_tensordicts")
        assert len(a0._locked_tensordicts)
        assert len(b0._locked_tensordicts)
        td.unlock_()
        assert not len(a0._locked_tensordicts)
        assert not len(b0._locked_tensordicts)
        assert not len(c0._locked_tensordicts)

    def test_stack_cache_lock(self):
        td0 = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td1 = td0.clone()
        td = torch.stack([td0, td1])
        assert td._is_locked is None
        td = td.lock_()
        assert td._is_locked
        td.unlock_()
        assert td._is_locked is None
        td0.lock_()
        # all tds must be locked
        assert not td.is_locked
        # lock td1
        td1.lock_()
        # we can unlock td0, even though td is locked
        assert td.is_locked
        assert td._is_locked is None  # lock wasn't called on td
        td0.unlock_()
        td.unlock_()
        assert not td0.is_locked
        assert td._is_locked is None

        td.lock_()
        assert td1.is_locked
        assert td0.is_locked
        with pytest.raises(RuntimeError):
            td1.unlock_()
        assert td1.is_locked

        # create a parent to td
        super_td = TensorDict({"td": td}, [])
        super_td.lock_()
        assert td._is_locked
        super_td.unlock_()
        assert td._is_locked is None

    def test_stacked_append_and_insert(self):
        td0 = TensorDict({("a", "b", "c", "d"): 1.0}, [])
        td1 = td0.clone()
        td = torch.stack([td0, td1])
        td.lock_()
        with pytest.raises(RuntimeError, match=re.escape(TensorDictBase.LOCK_ERROR)):
            td.insert(0, td0)
        with pytest.raises(RuntimeError, match=re.escape(TensorDictBase.LOCK_ERROR)):
            td.append(td0)
        td.unlock_()
        td.insert(0, td0)
        td.append(td0)


@pytest.mark.parametrize("memmap", [True, False])
@pytest.mark.parametrize("params", [False, True])
def test_from_module(memmap, params):
    net = nn.Transformer(
        d_model=16,
        nhead=2,
        num_encoder_layers=3,
        dim_feedforward=12,
    )
    td = TensorDict.from_module(net, as_module=params)
    # check that we have empty tensordicts, reflecting modules wihout params
    for subtd in td.values(True):
        if isinstance(subtd, TensorDictBase) and subtd.is_empty():
            break
    else:
        raise RuntimeError
    if memmap:
        td = td.detach().memmap_()
    net.load_state_dict(td.flatten_keys("."))

    if not memmap and params:
        assert set(td.parameters()) == set(net.parameters())


@pytest.mark.parametrize("batch_size", [None, [3, 4]])
@pytest.mark.parametrize("batch_dims", [None, 1, 2])
@pytest.mark.parametrize("device", get_available_devices())
def test_from_dict(batch_size, batch_dims, device):
    data = {
        "a": torch.zeros(3, 4, 5),
        "b": {"c": torch.zeros(3, 4, 5, 6)},
        ("d", "e"): torch.ones(3, 4, 5),
        ("b", "f"): torch.zeros(3, 4, 5, 5),
        ("d", "g", "h"): torch.ones(3, 4, 5),
    }
    if batch_dims and batch_size:
        with pytest.raises(ValueError, match="both"):
            TensorDict.from_dict(
                data, batch_size=batch_size, batch_dims=batch_dims, device=device
            )
        return
    data = TensorDict.from_dict(
        data, batch_size=batch_size, batch_dims=batch_dims, device=device
    )
    assert data.device == device
    assert "a" in data.keys()
    assert ("b", "c") in data.keys(True)
    assert ("b", "f") in data.keys(True)
    assert ("d", "e") in data.keys(True)
    assert data.device == device
    if batch_dims:
        assert data.ndim == batch_dims
        assert data["b"].ndim == batch_dims
        assert data["d"].ndim == batch_dims
        assert data["d", "g"].ndim == batch_dims
    elif batch_size:
        assert data.batch_size == torch.Size(batch_size)
        assert data["b"].batch_size == torch.Size(batch_size)
        assert data["d"].batch_size == torch.Size(batch_size)
        assert data["d", "g"].batch_size == torch.Size(batch_size)


def test_unbind_batchsize():
    td = TensorDict({"a": TensorDict({"b": torch.zeros(2, 3)}, [2, 3])}, [2])
    td["a"].batch_size
    tds = td.unbind(0)
    assert tds[0].batch_size == torch.Size([])
    assert tds[0]["a"].batch_size == torch.Size([3])


def test_empty():
    td = TensorDict(
        {
            "a": torch.zeros(()),
            ("b", "c"): torch.zeros(()),
            ("b", "d", "e"): torch.zeros(()),
        },
        [],
    )
    td_empty = td.empty(recurse=False)
    assert len(list(td_empty.keys())) == 0
    td_empty = td.empty(recurse=True)
    assert len(list(td_empty.keys())) == 1
    assert len(list(td_empty.get("b").keys())) == 1


@pytest.mark.parametrize(
    "stack_dim",
    [0, 1, 2, 3],
)
@pytest.mark.parametrize(
    "nested_stack_dim",
    [0, 1, 2],
)
def test_dense_stack_tds(stack_dim, nested_stack_dim):
    batch_size = (5, 6)
    td0 = TensorDict(
        {"a": torch.zeros(*batch_size, 3)},
        batch_size,
    )
    td1 = TensorDict(
        {"a": torch.zeros(*batch_size, 4), "b": torch.zeros(*batch_size, 2)},
        batch_size,
    )
    td_lazy = torch.stack([td0, td1], dim=nested_stack_dim)
    td_container = TensorDict({"lazy": td_lazy}, td_lazy.batch_size)
    td_container_clone = td_container.clone()
    td_container_clone.apply_(lambda x: x + 1)

    assert td_lazy.stack_dim == nested_stack_dim
    td_stack = torch.stack([td_container, td_container_clone], dim=stack_dim)
    assert td_stack.stack_dim == stack_dim

    assert isinstance(td_stack, LazyStackedTensorDict)
    dense_td_stack = dense_stack_tds(td_stack)
    assert isinstance(dense_td_stack, TensorDict)  # check outer layer is non-lazy
    assert isinstance(
        dense_td_stack["lazy"], LazyStackedTensorDict
    )  # while inner layer is still lazy
    assert "b" not in dense_td_stack["lazy"].tensordicts[0].keys()
    assert "b" in dense_td_stack["lazy"].tensordicts[1].keys()

    assert assert_allclose_td(
        dense_td_stack,
        dense_stack_tds([td_container, td_container_clone], dim=stack_dim),
    )  # This shows it is the same to pass a list or a LazyStackedTensorDict

    for i in range(2):
        index = (slice(None),) * stack_dim + (i,)
        assert (dense_td_stack[index] == i).all()

    if stack_dim > nested_stack_dim:
        assert dense_td_stack["lazy"].stack_dim == nested_stack_dim
    else:
        assert dense_td_stack["lazy"].stack_dim == nested_stack_dim + 1


@pytest.mark.parametrize(
    "td_name",
    [
        "td",
        "stacked_td",
        "sub_td",
        "sub_td2",
        "idx_td",
        "memmap_td",
        "unsqueezed_td",
        "squeezed_td",
        "td_reset_bs",
        "nested_td",
        "nested_tensorclass",
        "permute_td",
        "nested_stacked_td",
        "td_params",
        pytest.param(
            "td_h5", marks=pytest.mark.skipif(not _has_h5py, reason="h5py not found.")
        ),
    ],
)
@pytest.mark.parametrize("device", get_available_devices())
class TestTensorDictMP(TestTensorDictsBase):
    @staticmethod
    def add1(x):
        return x + 1

    @staticmethod
    def add1_app(x):
        return x.apply(lambda x: x + 1)

    @staticmethod
    def add1_app_error(x):
        # algerbraic ops are not supported
        return x + 1

    @staticmethod
    def write_pid(x):
        return TensorDict({"pid": os.getpid()}, []).expand(x.shape)

    @pytest.mark.parametrize("dim", [-2, -1, 0, 1, 2, 3])
    def test_map(self, td_name, device, dim, _pool_fixt):
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            with pytest.raises(
                RuntimeError, match="Cannot call map on a TensorDictParams object"
            ):
                td.map(self.add1_app, dim=dim, pool=_pool_fixt)
            return
        assert (
            td.map(self.add1_app, dim=dim, pool=_pool_fixt) == td.apply(self.add1)
        ).all()

    @pytest.mark.parametrize("dim", [-2, -1, 0, 1, 2, 3])
    def test_map_exception(self, td_name, device, dim, _pool_fixt):
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            with pytest.raises(
                RuntimeError, match="Cannot call map on a TensorDictParams object"
            ):
                td.map(self.add1_app_error, dim=dim, pool=_pool_fixt)
            return
        with pytest.raises(TypeError, match="unsupported operand"):
            td.map(self.add1_app_error, dim=dim, pool=_pool_fixt)

    @pytest.mark.parametrize(
        "chunksize,num_chunks", [[None, 2], [4, None], [None, None], [2, 2]]
    )
    def test_chunksize_num_chunks(
        self, td_name, device, chunksize, num_chunks, _pool_fixt, dim=0
    ):
        td = getattr(self, td_name)(device)
        if td_name == "td_params":
            with pytest.raises(
                RuntimeError, match="Cannot call map on a TensorDictParams object"
            ):
                td.map(self.add1_app_error, dim=dim, pool=_pool_fixt)
            return
        if chunksize is not None and num_chunks is not None:
            with pytest.raises(ValueError, match="but not both"):
                td.map(
                    self.write_pid,
                    dim=dim,
                    chunksize=chunksize,
                    num_chunks=num_chunks,
                    pool=_pool_fixt,
                )
            return
        mapped = td.map(
            self.write_pid,
            dim=dim,
            chunksize=chunksize,
            num_chunks=num_chunks,
            pool=_pool_fixt,
        )
        pids = mapped.get("pid").unique()
        if chunksize is not None:
            assert pids.numel() == -(td.shape[0] // -chunksize)
        elif num_chunks is not None:
            assert pids.numel() == num_chunks


@pytest.fixture(scope="class")
def _pool_fixt():
    with mp.Pool(10) as pool:
        yield pool


class TestFCD(TestTensorDictsBase):
    """Test stack for first-class dimension."""

    @pytest.mark.parametrize(
        "td_name",
        [
            "td",
            "stacked_td",
            "sub_td",
            "sub_td2",
            "idx_td",
            "memmap_td",
            "unsqueezed_td",
            "squeezed_td",
            "td_reset_bs",
            "nested_td",
            "nested_tensorclass",
            "permute_td",
            "nested_stacked_td",
            "td_params",
            pytest.param(
                "td_h5",
                marks=pytest.mark.skipif(not _has_h5py, reason="h5py not found."),
            ),
        ],
    )
    @pytest.mark.parametrize("device", get_available_devices())
    def test_fcd(self, td_name, device):
        td = getattr(self, td_name)(device)
        d0 = ftdim.dims(1)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim == 0:
            with pytest.raises(ValueError, match="Cannot index"):
                td[d0]
        else:
            assert td[d0].shape == td.shape[1:]
        d0, d1 = ftdim.dims(2)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim in (0, 1):
            with pytest.raises(ValueError, match="Cannot index"):
                td[d0, d1]
        else:
            assert td[d0, d1].shape == td.shape[2:]
        d0, d1, d2 = ftdim.dims(3)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim in (0, 1, 2):
            with pytest.raises(ValueError, match="Cannot index"):
                td[d0, d1, d2]
        else:
            assert td[d0, d1, d2].shape == td.shape[3:]
        d0 = ftdim.dims(1)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim == 1:
            with pytest.raises(ValueError, match="Cannot index"):
                td[:, d0]
        else:
            assert td[:, d0].shape == torch.Size((td.shape[0], *td.shape[2:]))

    @pytest.mark.parametrize(
        "td_name",
        [
            "td",
            "stacked_td",
            "idx_td",
            "memmap_td",
            "td_reset_bs",
            "nested_td",
            "nested_tensorclass",
            "nested_stacked_td",
            "td_params",
            pytest.param(
                "td_h5",
                marks=pytest.mark.skipif(not _has_h5py, reason="h5py not found."),
            ),
            # these tds cannot see their dim names edited:
            # "sub_td",
            # "sub_td2",
            # "unsqueezed_td",
            # "squeezed_td",
            # "permute_td",
        ],
    )
    @pytest.mark.parametrize("device", get_available_devices())
    def test_fcd_names(self, td_name, device):
        td = getattr(self, td_name)(device)
        td.names = ["a", "b", "c", "d"]
        d0 = ftdim.dims(1)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim == 0:
            with pytest.raises(ValueError, match="Cannot index"):
                td[d0]
        else:
            assert td[d0].names == ["b", "c", "d"]
        d0, d1 = ftdim.dims(2)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim in (0, 1):
            with pytest.raises(ValueError, match="Cannot index"):
                td[d0, d1]
        else:
            assert td[d0, d1].names == ["c", "d"]
        d0, d1, d2 = ftdim.dims(3)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim in (0, 1, 2):
            with pytest.raises(ValueError, match="Cannot index"):
                td[d0, d1, d2]
        else:
            assert td[d0, d1, d2].names == ["d"]
        d0 = ftdim.dims(1)
        if isinstance(td, LazyStackedTensorDict) and td.stack_dim == 1:
            with pytest.raises(ValueError, match="Cannot index"):
                td[:, d0]
        else:
            assert td[:, d0].names == ["a", "c", "d"]

    @pytest.mark.parametrize("as_module", [False, True])
    def test_modules(self, as_module):
        modules = [
            lambda: nn.Linear(3, 4),
            lambda: nn.Sequential(nn.Linear(3, 4), nn.Linear(4, 4)),
            lambda: nn.Transformer(16, 4, 2, 2, 8),
            lambda: nn.Sequential(nn.Conv2d(3, 4, 3), nn.Conv2d(4, 4, 3)),
        ]
        inputs = [
            lambda: (torch.randn(2, 3),),
            lambda: (torch.randn(2, 3),),
            lambda: (torch.randn(2, 3, 16), torch.randn(2, 3, 16)),
            lambda: (torch.randn(2, 3, 16, 16),),
        ]
        param_batch = 5
        for make_module, make_input in zip(modules, inputs):
            module = make_module()
            td = TensorDict.from_module(module, as_module=as_module)
            td = td.expand(param_batch).clone()
            d0 = ftdim.dims(1)
            td = TensorDictParams(td)[d0]
            td.to_module(module)
            y = module(*make_input())
            assert y.dims == (d0,)
            assert y._tensor.shape[0] == param_batch


if __name__ == "__main__":
    args, unknown = argparse.ArgumentParser().parse_known_args()
    pytest.main([__file__, "--capture", "no", "--exitfirst"] + unknown)
