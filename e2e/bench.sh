#!/bin/zsh

python -m benchmarks.bench_textgen --batch-size 1 --num-batches 20
python -m benchmarks.bench_textgen --batch-size 4 --num-batches 20
python -m benchmarks.bench_textgen --batch-size 16 --num-batches 20
python -m benchmarks.bench_textgen --batch-size 64 --num-batches 20
python -m benchmarks.bench_textgen --batch-size 256 --num-batches 20
