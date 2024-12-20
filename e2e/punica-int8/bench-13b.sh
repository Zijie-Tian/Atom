#!/bin/zsh

# Enable ctrl+c to kill all child processes
trap 'kill $(jobs -p)' INT

# Set GPU device
GPU_ID=3

# Batch size 1
python -m benchmarks.bench_textgen --batch-size 1 --num-batches 20 --model 13b --device cuda:$GPU_ID &
sleep 15
nvidia-smi -i $GPU_ID --query-gpu=memory.used --format=csv,noheader,nounits
wait || exit 1

# Batch size 4
python -m benchmarks.bench_textgen --batch-size 4 --num-batches 20 --model 13b --device cuda:$GPU_ID &
sleep 15
nvidia-smi -i $GPU_ID --query-gpu=memory.used --format=csv,noheader,nounits
wait || exit 1

# Batch size 16
python -m benchmarks.bench_textgen --batch-size 16 --num-batches 20 --model 13b --device cuda:$GPU_ID &
sleep 15
nvidia-smi -i $GPU_ID --query-gpu=memory.used --format=csv,noheader,nounits
wait || exit 1

# Batch size 64
python -m benchmarks.bench_textgen --batch-size 64 --num-batches 20 --model 13b --device cuda:$GPU_ID &
sleep 15
nvidia-smi -i $GPU_ID --query-gpu=memory.used --format=csv,noheader,nounits
wait || exit 1

# Batch size 256
python -m benchmarks.bench_textgen --batch-size 256 --num-batches 20 --model 13b --device cuda:$GPU_ID &
sleep 15
nvidia-smi -i $GPU_ID --query-gpu=memory.used --format=csv,noheader,nounits
wait || exit 1
