rm -rf build
mkdir build
cd build

# Fill in your libtorch path
cmake .. -DCMAKE_PREFIX_PATH=/home/tzj/Quant/Atom/kernels/3rdparty/libtorch
make -j