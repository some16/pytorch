import torch


def create_blocked_tensor(B, M, N, blocksize, sparsity, dtype, device):
    assert (
        sparsity <= 1.0 and sparsity >= 0.0
    ), "sparsity should be a value between 0 and 1"
    assert M % blocksize[0] == 0
    assert N % blocksize[1] == 0
    shape = (B, M // blocksize[0], N // blocksize[1])[int(B == 0) :]
    A = torch.bernoulli(torch.full(shape, 1 - sparsity, dtype=dtype, device=device))
    A = torch.repeat_interleave(A, blocksize[0], dim=-2)
    A = torch.repeat_interleave(A, blocksize[1], dim=-1)
    return A


def _test_worker(test_func):
    import triton

    ms, ms_min, ms_max = triton.testing.do_bench(
        test_func, warmup=500, rep=100, fast_flush=False
    )

    tflops = 2 * m * k * n * 1e-12 / (ms * 1e-3)
    return ms, tflops


def test_dense_dense_mm(x, y, **meta):
    def test_func(x=x.to_dense(), y=y):
        return torch.matmul(x, y)

    return _test_worker(test_func)


def test_torch_matmul(x, y, **meta):
    def test_func(x=x, y=y):
        return torch.matmul(x, y)

    return _test_worker(test_func)


def test_bsr_dense_mm(x, y, **meta):
    from torch.sparse._triton_ops import bsr_dense_mm

    def test_func(x=x, y=y):
        return bsr_dense_mm(x, y)

    return _test_worker(test_func)


def test_bsr_scatter_mm2(x, y, **meta):
    from torch.sparse._triton_ops import bsr_scatter_mm, bsr_scatter_mm_indices_data

    indices_data = dict()
    indices_data.update(tasks2=bsr_scatter_mm_indices_data(x, y)["tasks2"])

    def test_func(x=x, y=y):
        return bsr_scatter_mm(x, y, indices_data=indices_data)

    return _test_worker(test_func)


def test_bsr_scatter_mm6(x, y, **meta):
    from torch.sparse._triton_ops import bsr_scatter_mm, bsr_scatter_mm_indices_data

    indices_data = bsr_scatter_mm_indices_data(
        x, y, indices_format="bsr_strided_mm_compressed", **meta
    )

    def test_func(x=x, y=y):
        return bsr_scatter_mm(x, y, indices_data=indices_data)

    return _test_worker(test_func)


if __name__ == "__main__":
    import argparse
    import itertools
    import sys

    import triton
    from torch.testing import make_tensor

    torch.manual_seed(0)

    def integer_list(a):
        return list(map(int, a.split(",")))

    def float_list(a):
        return list(map(float, a.split(",")))

    parser = argparse.ArgumentParser(description="SpTritonOps")

    parser.add_argument(
        "--ops",
        default="dense_dense_mm,bsr_dense_mm,bsr_scatter_mm6",
        type=str,
    )
    parser.add_argument("--b", default="0", type=int)

    parser.add_argument("--m", default="1024", type=integer_list)
    parser.add_argument("--k", default=None, type=integer_list)
    parser.add_argument("--n", default=None, type=integer_list)
    parser.add_argument("--bm", default="16", type=integer_list)
    parser.add_argument("--bk", default=None, type=integer_list)
    parser.add_argument("--tile_m", default=None, type=integer_list)
    parser.add_argument("--tile_n", default=None, type=integer_list)
    parser.add_argument("--split_n", default=None, type=integer_list)
    parser.add_argument("--group_size", default=None, type=integer_list)
    parser.add_argument("--num_warps", default=None, type=integer_list)
    parser.add_argument("--num_stages", default=None, type=integer_list)
    parser.add_argument("--sparsity", "--sparsity", default="0.5", type=float_list)
    parser.add_argument("--dtype", default="float16", type=str)
    parser.add_argument("--device", default="cuda", type=str)
    parser.add_argument("--repeat", default="1", type=int)
    parser.add_argument("--outfile", default="stdout", type=str)

    args = parser.parse_args()

    if args.outfile == "stdout":
        outfile = sys.stdout
    elif args.outfile == "stderr":
        outfile = sys.stderr
    else:
        outfile = open(args.outfile, "a")

    ops = args.ops.split(",")

    b = args.b
    m_list = args.m or [1024]
    n_list = args.n or [None]
    k_list = args.k or [None]
    bm_list = args.bm or [16]
    bk_list = args.bk or [None]
    split_n_list = args.split_n or [None]
    tile_m_list = args.tile_m or [None]
    tile_n_list = args.tile_n or [None]
    group_size_list = args.group_size or [None]
    num_warps_list = args.num_warps or [None]
    num_stages_list = args.num_stages or [None]
    sparsity_list = args.sparsity or [0.5]

    dtype = getattr(torch, args.dtype)
    device = args.device

    for m, k, n, bm, bk, sparsity in itertools.product(
        m_list, n_list, k_list, bm_list, bk_list, sparsity_list
    ):
        k = k or m
        n = n or m
        bk = bk or bm

        if bm > m or bk > k:
            continue

        blocksize = (bm, bk)

        x = create_blocked_tensor(
            b, m, k, blocksize, sparsity, dtype, device
        ).to_sparse_bsr(blocksize)

        y = make_tensor(k, n, dtype=dtype, device=device)

        bsr_size = f"{b}x{m}x{k}" if b > 0 else f"{k}x{n}"

        for op in ops:
            best_tflops = 0
            for (
                split_n,
                num_warps,
                num_stages,
                tile_m,
                tile_n,
                group_size,
            ) in itertools.product(
                split_n_list,
                num_warps_list,
                num_stages_list,
                tile_m_list,
                tile_n_list,
                group_size_list,
            ):
                if (
                    (tile_m or 0) > bm
                    or (tile_n or 0) > n // (split_n or 1)
                    or n % (split_n or 1) != 0
                    or (split_n or 0) > n
                ):
                    continue
                test_func = globals()["test_" + op]
                meta = (
                    dict(
                        SPLIT_N=split_n,
                        TILE_M=tile_m,
                        TILE_N=tile_n,
                        GROUP_SIZE=group_size,
                        num_stages=num_stages,
                        num_warps=num_warps,
                    )
                    if op == "bsr_scatter_mm6"
                    else dict()
                )

                meta_str = ";".join(f"{k}={v}" for k, v in meta.items())
                time_ms_lst = []
                performance_tflops_lst = []
                for r in range(args.repeat):
                    try:
                        time_ms, performance_tflops = test_func(x, y, **meta)
                    except triton.compiler.OutOfResources as msg:
                        print(
                            f"op={op}[{meta_str}]({bsr_size},{k}x{n}) dtype={args.dtype} {sparsity=}(nnz={x._nnz()})"
                            f" blocksize={bm}x{bk} OutOfResources",
                            file=outfile,
                        )
                        continue
                    except Exception as msg:
                        msg = str(msg).split("\n", 1)[0]
                        print(
                            f"op={op}[{meta_str}]({bsr_size},{k}x{n}) dtype={args.dtype} {sparsity=}(nnz={x._nnz()})"
                            f" blocksize={bm}x{bk} {msg}",
                            file=outfile,
                        )
                        continue
                    time_ms_lst.append(time_ms)
                    performance_tflops_lst.append(performance_tflops)

                    mark = ""
                    if best_tflops < performance_tflops:
                        best_tflops = performance_tflops
                        mark = " !!!"
                    print(
                        f"op={op}[{meta_str}]({bsr_size},{k}x{n}) dtype={args.dtype} {sparsity=}(nnz={x._nnz()})"
                        f" blocksize={bm}x{bk}"
                        f" time={time_ms:.3f} ms performance={performance_tflops:.3f} TFLOPS{mark}",
                        file=outfile,
                    )
                    outfile.flush()
                if args.repeat > 1:
                    avg_time_ms = sum(time_ms_lst) / len(time_ms_lst)
                    avg_performance_tflops = sum(performance_tflops_lst) / len(
                        performance_tflops_lst
                    )
                    print(
                        f"op={op}[{meta_str}]({bsr_size},{k}x{n}) dtype={args.dtype} {sparsity=}(nnz={x._nnz()})"
                        f" blocksize={bm}x{bk}"
                        f" time={time_ms:.3f} ms performance={performance_tflops:.3f} TFLOPS [AVERAGE]",
                        file=outfile,
                    )
                    outfile.flush()
                if op not in {"bsr_scatter_mm6"}:
                    break
