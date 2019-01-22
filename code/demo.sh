# parameters for sketching
NUM_SKETCHES=1
NUM_QUANTILES=1000
NUM_THETAS=2000
CLIPTO=30000

# parameters for auto encoder
BOTTLENECK_SIZE=32
AE_STRING="--ae"

# parameters for SWF
STEPSIZE=50
REG=0

# parameters for particles
NUM_SAMPLES=5000
# particles type is either TESTSET or RANDOM. If RANDOM, the RANDOM_INPUT_DIM
# controls the dimension of the input
PARTICLES_TYPE='RANDOM'
INPUT_DIM=16

# number of test
NUM_TEST=100
TEST_TYPE='INTERPOLATE'

# plot every
PLOT_EVERY=100

if [ $1 = "toy.npy" ]; then
  echo "generating toy data, and then SWF on it"

  # Generate data:
  NUM_SAMPLES_TOY=50000
  NUM_COMPONENTS_TOY=5
  SEED_STRING="--seed 10"

  python ./generate_toydata.py  --output toy --dim $INPUT_DIM --num_samples $NUM_SAMPLES_TOY --num_components $NUM_COMPONENTS_TOY $SEED_STRING
fi

# now launch the sliced Wasserstein flow
python swf.py $1 --root_data_dir ~/data/$1 --img_size 32 --num_sketches $NUM_SKETCHES --clip $CLIPTO --num_quantiles $NUM_QUANTILES --input_dim $INPUT_DIM  --num_samples $NUM_SAMPLES --particles_type=$PARTICLES_TYPE --stepsize $STEPSIZE --num_thetas $NUM_THETAS --plot_dir ~/swf_samples_$1 --logdir logs/$1 --plot_every $PLOT_EVERY $AE_STRING --bottleneck_size $BOTTLENECK_SIZE --ae_model ae --num_test $NUM_TEST --test_type $TEST_TYPE
