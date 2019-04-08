# imports
import os
import torch
from torch import nn
from torchvision.utils import save_image
from torchvision.utils import make_grid
import qsketch
import data
import argparse
import functools
import torch.multiprocessing as mp
import networks
from torchinterp1d import Interp1d
from torchpercentile import Percentile
import utils
from math import sqrt


def swf(train_particles, test_particles, target_stream, modules,
        stepsize, regularization, num_epochs,
        device_str, logger, results_path="results"):
    """Starts a Sliced Wasserstein Flow with the train_particles, to match
    the distribution whose sketches are given by the target queue.

    The function gets sketches from the queue, and then applies steps of a
    SWF to the particles. The flow is parameterized by a stepsize and a
    regularization parameter. """

    from tqdm import tqdm, trange

    # get the device
    device = torch.device(device_str)

    # prepare stuff
    criterion = nn.MSELoss()
    particles = {}
    particles['train'] = train_particles.to(device)
    if test_particles is not None:
        particles['test'] = test_particles.to(device)
    data_shape = train_particles[0].shape

    # pre-allocate variables
    step = {}
    step_weight = {}
    interp_q = {}
    transported = {}
    particles_qf = {}
    projections = {}
    loss = {}
    train_losses = []
    test_losses = []
    interp_q['train'] = None
    transported['train'] = None
    if test_particles is not None:
        interp_q['test'] = None
        transported['test'] = None

    data_queue = target_stream.queue
    projector = modules[0].to(device)
    percentiles = target_stream.percentiles.clone().to(device)
    bar_epoch = trange(num_epochs, desc="epoch")

    # call the logger with the transported train and test particles
    logger(bar_epoch, particles, -1, loss)

    # loop over epochs
    for epoch in bar_epoch:
        if target_stream.shared_data['num_epochs'] == 1:
            next_queue = mp.Queue()

        # reset the step for both train and test
        step['train'] = 0
        step['test'] = 0
        step_weight['train'] = 0
        step_weight['test'] = 0
        loss['train'] = 0
        loss['test'] = 0

        pbar = tqdm(total=target_stream.shared_data['num_sketches'])
        for (target_qf, id) in iter(data_queue.get, None):
            # get the data from the sketching queue
            if target_stream.shared_data['num_epochs'] == 1:
                next_queue.put((target_qf.detach().clone(), id))
            target_qf = target_qf.to(device)
            projector.recycle(id)

            # print('IN SWF',id, projector.weight[0,:5])
            # stepsize *= (index+1)/(index+2)
            for task in particles:  # will include test if provided
                # project the particles
                with torch.no_grad():
                    num_particles = particles[task].shape[0]
                    projections[task] = projector(particles[task])

                    # compute the corresponding quantiles
                    percentile_fn = Percentile()
                    particles_qf[task] = percentile_fn(projections[task],
                                                       percentiles)

                    # compute the loss: squared error over the quantiles
                    loss[task] += criterion(particles_qf[task], target_qf)

                    # transort the marginals
                    interp_q[task] = Interp1d()(x=particles_qf['train'].t(),
                                                y=percentiles,
                                                xnew=projections[task].t(),
                                                out=interp_q[task])

                    #interp_q[task] = torch.clamp(interp_q[task], 0, 100)
                    transported[task] = Interp1d()(x=percentiles,
                                                   y=target_qf.t(),
                                                   xnew=interp_q[task],
                                                   out=transported[task])
                    step_weight[task] += projections[task].shape[1]
                    step[task] += (
                        projector.backward(
                                transported[task].t() - projections[task])
                        .view(num_particles, *data_shape))
            pbar.update(1)

            # print('SWF: finish id', id)
        # print('SWF: updating particles')
        # we got all the updates with the sketches. Now apply the steps
        for task in particles:
            # first apply the step
            particles[task] += stepsize/step_weight[task]*step[task]

            # then possibly add the noise if needed
            noise = torch.randn(*particles[task].shape, device=device)
            noise /= sqrt(particles[task].shape[-1])
            particles[task] += regularization * noise

        if target_stream.shared_data['num_epochs'] == 1:
            next_queue.put(None)
            data_queue = next_queue

        # Now do some logging / plotting
        loss = logger(bar_epoch, particles, epoch, loss)
        train_losses.append(float(loss['train']))
        if test_particles is not None:
            test_losses.append(float(loss['test']))

        """print('SWF writing results')
        # Saving stuff on disk
        params = {
            'train_losses': [float(x) for x in train_losses],
            'test_losses': [float(x) for x in test_losses],
            'args': vars(args),
            'epochs': int(epoch)
        }
        uuids = uuid.uuid4().hex[:6]

        if not os.path.exists(results_path):
            os.mkdir(results_path)

        with open(Path(results_path,  uuids + ".json"), 'w') as outfile:
            outfile.write(json.dumps(params, indent=4, sort_keys=True))"""

    return (
        (particles['train'], particles['test']) if 'test' in particles
        else particles['train'])


def logger_function(writer, particles, index, loss,
                    plot_dir, plot_every, match_every,
                    img_shape, dataset, ae=None):
    """ Logging function."""

    loss_str = 'epoch %d: ' % (index + 1)
    for item, value in loss.items():
        loss_str += item + ': %0.12f ' % value
    writer.write(loss_str)

    match = match_every > 0 and index > 0 and not index % match_every
    plot = index < 0 or (plot_every > 0 and not index % plot_every)
    if not plot and not match:
        return loss

    # set the number of images we want to plot in the grid
    # for each image we add the closest match (so nb_of_images * 2)
    nb_of_images = 320

    # displays generated images and display closest match in dataset
    for task in particles:
        # if we use the autoencoder deocde the particles to visualize them
        if ae is not None:
            decoder = ae.model.decode.to(particles[task].device)
            cur_task = decoder(particles[task][:nb_of_images, ...])
            img_shape = ae.model.input_shape
        # otherwise just use the particles
        else:
            cur_task = particles[task][:nb_of_images, ...]

        if match:
            # create empty grid
            output_viewport = torch.zeros((nb_of_images,)
                                          + cur_task.shape[1:])

            writer.write("Finding closest matches in dataset")
            closest = utils.find_closest(particles[task][:nb_of_images],
                                         dataset)
            if ae is not None:
                output_viewport = decoder(closest.to(device))
            else:
                output_viewport = closest

            pic = make_grid(
                output_viewport.view(-1, *img_shape),
                nrow=16, padding=2, normalize=True, scale_each=True
            )

            if plot_dir is not None:
                # create the temporary folder for plotting generated samp
                if not os.path.exists(plot_dir):
                    os.mkdir(plot_dir)
                save_image(
                    pic,
                    '{}/{}_image_match_{}.png'.format(
                        plot_dir, task, index
                    )
                )

        output_viewport = cur_task

        pic = make_grid(
            output_viewport.view(-1, *img_shape),
            nrow=16, padding=2, normalize=True, scale_each=True
        )

        if plot_dir is not None:
            # create the temporary folder for plotting generated samples
            if not os.path.exists(plot_dir):
                os.mkdir(plot_dir)
            save_image(pic,
                       '{}/{}_image_{}.png'.format(plot_dir, task, index))
    return loss


if __name__ == "__main__":
    mp.set_start_method('spawn', force=True)

    # create arguments parser and parse arguments
    parser = argparse.ArgumentParser(description='Sliced Wasserstein Flow.')
    parser = qsketch.add_sketch_arguments(parser)
    parser = data.add_data_arguments(parser)

    parser.add_argument("--input_dim",
                        help="Dimension of the random input to the "
                             "generative network. If negative, will "
                             "match the data dimension inferred from dataset",
                        type=int,
                        default=100)
    parser.add_argument("--num_thetas",
                        help="Number of thetas per sketch.",
                        type=int,
                        default=2000)
    parser.add_argument("--num_epochs",
                        help="Number of epochs",
                        type=int,
                        default=5000)
    parser.add_argument("--bottleneck_size",
                        help="Dimension of the bottleneck features",
                        type=int,
                        default=64)
    parser.add_argument("--num_samples",
                        help="Number of samples to draw per batch to "
                             "compute the sketch of that batch",
                        type=int,
                        default=3000)
    parser.add_argument("--stepsize",
                        help="Stepsize for the SWF",
                        type=float,
                        default=1e-3)
    parser.add_argument("--regularization",
                        help="Regularization term for the additive noise",
                        type=float,
                        default=0)
    parser.add_argument("--no_fixed_sketch",
                        help="If active, will generate new sketch at "
                             "each epoch",
                        action="store_true")
    parser.add_argument("--plot_every",
                        help="Number of iterations between each plot."
                             " Negative value means no plot",
                        type=int,
                        default=100)
    parser.add_argument("--match_every",
                        help="number of iteration between match with the "
                             "items in the database. Negative means this "
                             "is never checked.",
                        type=int,
                        default=-1)
    parser.add_argument("--plot_dir",
                        help="Output directory for the plots",
                        default="samples")
    parser.add_argument("--ae",
                        help="Activate reduction dimension through auto-"
                             "encoding.",
                        action="store_true")
    parser.add_argument("--conv_ae",
                        help="Activate convolutive AE",
                        action="store_true")
    parser.add_argument("--train_ae",
                        help="Force training of the AE.",
                        action="store_true")
    parser.add_argument("--ae_model",
                        help="filename for the autoencoder model")
    parser.add_argument("--num_test",
                        help="number of test samples",
                        type=int,
                        default=0)
    parser.add_argument("--test_type",
                        help="different kinds of test options. should be "
                             "either RANDOM or INTERPOLATE.")
    args = parser.parse_args()

    # prepare the torch device (cuda or cpu ?)
    device_str = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(device_str)

    # load the data
    train_data = data.load_image_dataset(
        dataset=args.dataset,
        data_dir=args.root_data_dir,
        img_size=args.img_size
    )

    # prepare AE
    if args.ae:
        autoencoder = networks.AE(
            train_data[0][0].shape,
            device=device,
            bottleneck_size=args.bottleneck_size,
            convolutive=args.conv_ae
        )
        ae_filename = os.path.join(
            'weights',
            args.ae_model
            + '%d' % args.bottleneck_size
            + ('conv' if args.conv_ae else 'dense')
            + '%d' % args.img_size
            + ''.join(e for e in args.dataset if e.isalnum())
            + '.model')

        print(ae_filename, 'number of bottleneck features:',
              args.bottleneck_size)

        if not os.path.exists(ae_filename) or args.train_ae:
            train_loader = torch.utils.data.DataLoader(
                train_data,
                batch_size=32,
                shuffle=True
            )
            print('training AE on', device)
            autoencoder.train(train_loader, nb_epochs=20)
            autoencoder.model = autoencoder.model.to('cpu')
            if args.ae_model is not None:
                torch.save(autoencoder.model.state_dict(), ae_filename)
        else:
            state = torch.load(ae_filename, map_location='cpu')
            autoencoder.model.to('cpu').load_state_dict(state)
            print("Model loaded")

        # augmenting the dataset with calling the encoder, to get an item
        train_data = data.TransformedDataset(
            train_data,
            transform=autoencoder.model.encode_nograd)

    data_shape = train_data[0][0].shape
    import seaborn as sb
    import matplotlib.pyplot as plt
    test = torch.cat([train_data[id][0] for id in range(50000)])
    plt.autoscale(True, tight=True)
    plt.clf()
    sb.set_style(sb.axes_style('whitegrid'))
    plot = sb.kdeplot(
        test[:, 0].numpy(),
        test[:, 1].numpy(),
        gridsize=200, n_levels=200,
        linewidths=0.1,
        clip=((-1, 12), (-1, 10))
        )
    plt.xlabel('feature 1')
    plt.ylabel('feature 2')
    plt.title('Distribution of bottleneck features on MNIST')
    plot.get_figure().savefig('test.pdf')

    import ipdb; ipdb.set_trace()

    # Launch the data stream
    data_stream = qsketch.DataStream(train_data)
    data_stream.stream()

    # prepare the sketcher
    target_stream = qsketch.Sketcher(data_source=data_stream,
                                     percentiles=torch.linspace(
                                            0, 100, args.num_quantiles),
                                     num_examples=args.num_examples)

    # prepare the projectors
    projectors = qsketch.ModulesDataset(
                        networks.LinearProjector,
                        shape_in=data_shape,
                        num_out=args.num_thetas)

    target_stream.stream(modules=projectors,
                         num_sketches=args.num_sketches,
                         num_epochs=(
                            args.num_epochs if args.no_fixed_sketch
                            else 1),
                         num_workers=args.num_workers)

    # generates the train particles
    print('using ', device)
    if args.input_dim < 0:
        input_shape = data_shape
    else:
        input_shape = [args.input_dim, ]

    train_particles = 0.5 + 0.1 * torch.rand(
        args.num_samples,
        *input_shape).to(device)

    # get the initial dimension for the train particles
    train_particles_shape = train_particles.shape[1:]

    # generate test particles
    if not args.num_test:
        test_particles = None
    elif args.test_type.upper() == "INTERPOLATE":
        # Create an interpolation between training particles
        nb_interp_test = 8
        nb_test_pic = 100
        interpolation = torch.linspace(0, 1, nb_interp_test).to(device)
        test_particles = torch.zeros(nb_interp_test * nb_test_pic,
                                     *train_particles_shape).to(device)
        for id in range(nb_test_pic):
            for id_in_q, q in enumerate(interpolation):
                test_particles[id*nb_interp_test+id_in_q, :] = (
                 q * train_particles[2*id+1] + (1-q)*train_particles[2*id])
    elif args.test_type.upper() == "RANDOM":
        test_particles = torch.randn(args.num_test,
                                     *train_particles_shape).to(device)
    else:
        raise Exception('test type must be either INTERPOLATE or RANDOM')

    print('Train particles dimension:', torch.tensor(
        train_particles_shape).numpy())

    # multiply them by a random matrix if not of the appropriate size
    if train_particles_shape != data_shape:
        print('Using a dimension augmentation matrix')
        torch.manual_seed(0)
        input_linear = torch.randn(
            torch.prod(torch.tensor(input_shape)),
            torch.prod(torch.tensor(data_shape))).to(device)
        train_particles = torch.mm(
            train_particles.view(args.num_samples, -1),
            input_linear)
        if test_particles is not None:
            test_particles = torch.mm(test_particles, input_linear)

    train_particles = train_particles.view(-1, *data_shape)
    if test_particles is not None:
        test_particles = test_particles.view(-1, *data_shape)

    # launch the sliced wasserstein flow
    particles = swf(train_particles=train_particles,
                    test_particles=test_particles,
                    target_stream=target_stream,
                    modules=projectors,
                    stepsize=args.stepsize,
                    regularization=args.regularization,
                    num_epochs=args.num_epochs,
                    device_str=device_str,
                    logger=functools.partial(
                                      logger_function,
                                      plot_dir=args.plot_dir,
                                      plot_every=args.plot_every,
                                      match_every=args.match_every,
                                      img_shape=data_shape,
                                      dataset=train_data,
                                      ae=(None if not args.ae
                                          else autoencoder)))
