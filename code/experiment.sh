parallel --bar -j 1 python swf.py {3} --root_data_dir /home/antoine/data/MNIST --img_size 32 --num_sketches 1 --clip 30000 --num_quantiles 100 --input_dim {2} --num_samples 3000 --particles_type=RANDOM --stepsize 1000 --num_thetas 16000 --plot_dir /home/antoine/swf_samples_MNIST  --plot_every -1 --match_every -1 --ae --conv_ae --bottleneck_size {1} --ae_model ae --num_test 3000 --test_type RANDOM --num_iter 5000 \
::: 16 32 48 64 80 \
::: 16 -1 \
::: MNIST FashionMNIST
