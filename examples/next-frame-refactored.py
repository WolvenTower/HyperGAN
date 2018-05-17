import os
import uuid
import random
import tensorflow as tf
import hypergan as hg
import hyperchamber as hc
import numpy as np
import glob
import time
import re
from hypergan.viewer import GlobalViewer
from hypergan.samplers.base_sampler import BaseSampler
from hypergan.gan_component import ValidationException, GANComponent
from hypergan.samplers.random_walk_sampler import RandomWalkSampler
from hypergan.samplers.debug_sampler import DebugSampler
from hypergan.search.alphagan_random_search import AlphaGANRandomSearch
from hypergan.gans.base_gan import BaseGAN
from common import *

import copy

from hypergan.gans.alpha_gan import AlphaGAN

from hypergan.gan_component import ValidationException, GANComponent
from hypergan.gans.base_gan import BaseGAN

from hypergan.discriminators.fully_connected_discriminator import FullyConnectedDiscriminator
from hypergan.encoders.uniform_encoder import UniformEncoder
from hypergan.trainers.multi_step_trainer import MultiStepTrainer
from hypergan.trainers.multi_trainer_trainer import MultiTrainerTrainer
from hypergan.trainers.consensus_trainer import ConsensusTrainer


arg_parser = ArgumentParser("render next frame")
parser = arg_parser.add_image_arguments()
parser.add_argument('--frames', type=int, default=4, help='Number of frames to embed.')
parser.add_argument('--shuffle', type=bool, default=False, help='Randomize inputs.')
args = arg_parser.parse_args()

width, height, channels = parse_size(args.size)

config = lookup_config(args)
if args.action == 'search':
    random_config = AlphaGANRandomSearch({}).random_config()
    if args.config_list is not None:
        config = random_config_from_list(args.config_list)

        config["generator"]=random_config["generator"]
        config["g_encoder"]=random_config["g_encoder"]
        config["discriminator"]=random_config["discriminator"]
        config["z_discriminator"]=random_config["z_discriminator"]

        # TODO Other search terms?
    else:
        config = random_config


def tryint(s):
    try:
        return int(s)
    except ValueError:
        return s

def alphanum_key(s):
    return [tryint(c) for c in re.split('([0-9]+)', s)]

class VideoFrameLoader:
    """
    """

    def __init__(self, batch_size, frame_count, shuffle):
        self.batch_size = batch_size
        self.frame_count = frame_count
        self.shuffle = shuffle

    def create(self, directory, channels=3, format='jpg', width=64, height=64, crop=False, resize=False):
        directories = glob.glob(directory+"/*")
        directories = [d for d in directories if os.path.isdir(d)]

        if(len(directories) == 0):
            directories = [directory] 

        # Create a queue that produces the filenames to read.
        if(len(directories) == 1):
            # No subdirectories, use all the images in the passed in path
            filenames = glob.glob(directory+"/*."+format)
        else:
            filenames = glob.glob(directory+"/**/*."+format)

        if(len(filenames) < self.frame_count):
            print("Error: Not enough frames in data folder ", directory)

        self.file_count = len(filenames)
        filenames = sorted(filenames, key=alphanum_key)
        if self.file_count == 0:
            raise ValidationException("No images found in '" + directory + "'")


        # creates arrays of filenames[:end], filenames[1:end-1], etc for serialized random batching
        if self.shuffle:
            frames  = [tf.train.slice_input_producer([filenames], shuffle=True)[0] for i in range(self.frame_count)]
        else:
            input_t = [filenames[i:i-self.frame_count] for i in range(self.frame_count)]
            input_queue = tf.train.slice_input_producer(input_t, shuffle=True)
            frames = input_queue

        # Read examples from files in the filename queue.
        frames = [self.read_frame(frame, format, crop, resize) for frame in frames]
        frames = self._get_data(frames)
        self.frames = frames

        x  = tf.train.slice_input_producer([filenames], shuffle=True)[0]
        y  = tf.train.slice_input_producer([filenames], shuffle=True)[0]
        self.x = self.read_frame(x, format, crop, resize)
        self.y = self.read_frame(y, format, crop, resize)
        self.x = self._get_data([self.x])
        self.y = self._get_data([self.y])


    def read_frame(self, t, format, crop, resize):
        value = tf.read_file(t)

        if format == 'jpg':
            img = tf.image.decode_jpeg(value, channels=channels)
        elif format == 'png':
            img = tf.image.decode_png(value, channels=channels)
        else:
            print("[loader] Failed to load format", format)
        img = tf.cast(img, tf.float32)


      # Image processing for evaluation.
      # Crop the central [height, width] of the image.
        if crop:
            resized_image = hypergan.inputs.resize_image_patch.resize_image_with_crop_or_pad(img, height, width, dynamic_shape=True)
        elif resize:
            resized_image = tf.image.resize_images(img, [height, width], 1)
        else: 
            resized_image = img

        tf.Tensor.set_shape(resized_image, [height,width,channels])

        # This moves the image to a range of -1 to 1.
        float_image = resized_image / 127.5 - 1.

        return float_image

    def _get_data(self, imgs):
        batch_size = self.batch_size
        num_preprocess_threads = 24
        return tf.train.shuffle_batch(
                imgs,
            batch_size=batch_size,
            num_threads=num_preprocess_threads,
            capacity= batch_size*2, min_after_dequeue=batch_size)
inputs = VideoFrameLoader(args.batch_size, args.frames, args.shuffle)
inputs.create(args.directory,
        channels=channels, 
        format=args.format,
        crop=args.crop,
        width=width,
        height=height,
        resize=True)

save_file = "save/model.ckpt"

class AliNextFrameGAN(BaseGAN):
    """ 
    """
    def __init__(self, *args, **kwargs):
        BaseGAN.__init__(self, *args, **kwargs)

    def required(self):
        """
        `input_encoder` is a discriminator.  It encodes X into Z
        `discriminator` is a standard discriminator.  It measures X, reconstruction of X, and G.
        `generator` produces two samples, input_encoder output and a known random distribution.
        """
        return "generator discriminator ".split()

    def create(self):
        config = self.config
        ops = self.ops

        with tf.device(self.device):
            def random_t(shape):
                shape[-1] //= len(config.z_distribution.projections)
                return UniformEncoder(self, config.z_distribution, output_shape=shape).sample
            def random_like(x):
                shape = self.ops.shape(x)
                return random_t(shape)

            self.frame_count = len(self.inputs.frames)
            self.frames = self.inputs.frames

            if config.same_g:
                z_g_prev_input = tf.concat(self.frames[1:-1], axis=3)
                z_g_prev = self.create_component(config.encoder, input=z_g_prev_input, name='prev_encoder')
            else:
                z_g_next_input = tf.concat(self.frames[:-1], axis=3)
                z_g_next = self.create_component(config.encoder, input=z_g_next_input, name='next_encoder')
                z_g_prev_input = tf.concat(self.frames[1:], axis=3)
                z_g_prev = self.create_component(config.encoder, input=z_g_prev_input, name='prev_encoder')
            target_prev = self.frames[0]
            target_next = self.frames[-1]

            if config.proxy_noise:
                pn_noise = [ops.shape(target_next)[0], config.proxy_z or 128]
                print("PN PROXY ", pn_noise)
                print('~~', random_t(pn_noise))
                pn_input = random_t(pn_noise)
                if config.proxy_feature:
                    proxy_noise = self.create_component(config.proxy_noise_generator, features=[z_g_prev.sample], input=pn_input, name='proxy_noise')
                else:
                    proxy_noise = self.create_component(config.proxy_noise_generator, input=pn_input, name='proxy_noise')
                z_noise = proxy_noise.sample
                if config.proxy_feature:
                    n_noise = self.create_component(config.proxy_noise_generator, features=[z_g_prev.sample], input=random_t(pn_noise), name='proxy_noise', reuse=True).sample
                else:
                    n_noise = self.create_component(config.proxy_noise_generator, input=random_t(pn_noise), name='proxy_noise', reuse=True).sample
                print("n_noise=", n_noise)
            else:
                z_noise = random_like(z_g_prev.sample)
                n_noise = random_like(z_g_prev.sample)

            if config.style:
                if config.same_g:
                    style_prev = self.create_component(config.style_encoder, input=self.inputs.frames[1], name='xb_style')
                    style_next = style_prev
                    gy_input = tf.concat(values=[z_g_prev.sample, n_noise], axis=3)
                    if config.skip_connections:
                        gen = self.create_component(config.generator, skip_connections=z_g_prev.layers, features=[style_prev.sample], input=gy_input, name='prev_generator')
                    else:
                        gen = self.create_component(config.generator, features=[style_prev.sample], input=gy_input, name='prev_generator')
                    gx_sample = tf.slice(gen.sample, [0,0,0,0], [-1,-1,-1,3])
                    gy_sample = tf.slice(gen.sample, [0,0,0,3], [-1,-1,-1,3])
                    gx = hc.Config({"sample":gx_sample})
                    gy = hc.Config({"sample":gy_sample})

                else:
                    style_prev = self.create_component(config.style_encoder, input=self.inputs.frames[-1], name='xb_style')
                    style_next = self.create_component(config.style_encoder, input=self.inputs.frames[0], name='xa_style')
                    gy_input = tf.concat(values=[z_g_prev.sample, n_noise], axis=3)
                    gy = self.create_component(config.generator, features=[style_prev.sample], input=gy_input, name='prev_generator')
                    gx_input = tf.concat(values=[z_g_next.sample, z_noise], axis=3)
                    gx = self.create_component(config.generator, features=[style_next.sample], input=gx_input, name='next_generator')
            else:
                if config.same_g:
                    if config.skip_connections:
                        gen = self.create_component(config.generator, skip_connections=z_g_prev.layers, features=[n_noise], input=z_g_prev.sample, name='prev_generator')
                    else:
                        print("Combine", z_g_prev.sample, n_noise)
                        gen = self.create_component(config.generator, features=[n_noise], input=z_g_prev.sample, name='prev_generator')
                    gx_sample = tf.slice(gen.sample, [0,0,0,0], [-1,-1,-1,3])
                    gy_sample = tf.slice(gen.sample, [0,0,0,3], [-1,-1,-1,3])
                    gx = hc.Config({"sample":gx_sample})
                    gy = hc.Config({"sample":gy_sample})
                    style_prev=hc.Config({"sample":random_like(z_g_prev.sample)})
                    style_next=hc.Config({"sample":random_like(z_g_prev.sample)})
                else:
                    gy = self.create_component(config.generator, features=[n_noise], input=z_g_prev.sample, name='prev_generator')
                    gx = self.create_component(config.generator, features=[z_noise], input=z_g_next.sample, name='next_generator')
                    style_prev=hc.Config({"sample":random_like(z_g_prev.sample)})
                    style_next=hc.Config({"sample":random_like(z_g_prev.sample)})

            self.y = hc.Config({"sample": target_prev})
            self.gy = gy
            self.gx = gx

            self.uniform_sample = gx.sample

            self.styleb = style_next
            self.stylea = style_prev
            self.random_style = random_like(style_prev.sample)

            t0 = target_next
            f0 = target_prev
            t1 = target_next
            t2 = gx.sample
            f1 = gy.sample
            f2 = target_prev
            stack = [t0, t1, t2]
            stacked = ops.concat(stack, axis=0)
            features = ops.concat([f0, f1, f2], axis=0)

            if config.same_g:
                g_vars1 = gen.variables()+z_g_prev.variables()
            else:
                g_vars1 = gx.variables()+gy.variables()+z_g_next.variables()+z_g_prev.variables()


            if config.g_inv:
                pn_input = random_t(pn_noise)
                p_noise = self.create_component(config.proxy_noise_generator, input=pn_input, name='p_noise')
 
                end_input = tf.concat([gy.sample, target_next], axis=3)
                end_encoder = self.create_component(config.encoder, input=end_input, name='inner_encoder')
                g_inner_frames = self.create_component(config.inner_generator, features=[p_noise.sample], input=end_encoder.sample, name='inner_generator')
                f2 = tf.concat(self.frames[0:-1], axis=3)
                f1 = tf.concat([f1, g_inner_frames.sample], axis=3)
                f0 =  tf.concat(self.frames[0:-1], axis=3)

                #self.inner_frames = []
                #for i in range(ops.shape(f0)[-1]//3):
                #    self.inner_frames.append(tf.slice(g_inner_frames.sample, [0,0,0,i*3],[-1,-1,-1,3]))

            if config.skip_real:

                stack = [t1, t2]
                stacked = ops.concat(stack, axis=0)
                features = ops.concat([f1, f2], axis=0)
            elif config.random_sample:
                gn_input = random_t([ops.shape(target_next)[0], config.proxy_z or 128])
                g_noise = self.create_component(config.proxy_noise_generator, features=[z_noise], input=gn_input, name='g_noise')
               
                if config.style:
                    s_noise = self.create_component(config.proxy_noise_generator, input=random_like(style_next.sample), name='s_noise')
                    g_input = tf.concat(values=[g_noise.sample, z_noise], axis=3)
                    gen = self.create_component(config.generator, features=[s_noise.sample], input=g_input, name='prev_generator', reuse=True)
                else:
                    print("CREATING", g_noise.sample, z_noise)
                    gen = self.create_component(config.generator, features=[z_noise], input=g_noise.sample, name='prev_generator', reuse=True)
                g_n = tf.slice(gen.sample, [0,0,0,0], [-1,-1,-1,3])
                g_p = tf.slice(gen.sample, [0,0,0,3], [-1,-1,-1,3])
                t3 = g_n
                f3 = g_p
                if config.g_inv:

                    pn_input = random_t(pn_noise)
                    p_noise = self.create_component(config.proxy_noise_generator, input=pn_input, name='p_noise', reuse=True)
     
                    end_input = tf.concat([g_p, g_n], axis=3)
                    end_encoder = self.create_component(config.encoder, input=end_input, name='inner_encoder', reuse=True)
                    g_inner_frames = self.create_component(config.inner_generator, features=[p_noise.sample], input=end_encoder.sample, name='inner_generator', reuse=True)
                    f3 = tf.concat([g_p, g_inner_frames.sample], axis=3)
                    # add t+1 to t+k-1 elements to joint mapping
                if config.include_real:
                    stack = [t0,t1, t2, t3]
                    stacked = ops.concat(stack, axis=0)
                    features = ops.concat([f0, f1, f2, f3], axis=0)
                else:
                    stack = [t1, t2, t3]
                    stacked = ops.concat(stack, axis=0)
                    features = ops.concat([f1, f2, f3], axis=0)
                g_vars1 += g_noise.variables()

                if config.style:
                    g_vars1 += s_noise.variables()

            d = self.create_component(config.discriminator, name='d_ab', input=stacked, features=[features])
            l = self.create_loss(config.loss, d, None, None, len(stack))
            loss1 = l
            d_loss1 = l.d_loss
            g_loss1 = l.g_loss

            d_vars1 = d.variables()

            if config.proxy_noise:
                g_vars1 += proxy_noise.variables()

            if config.g_inv:
                g_vars1 += g_inner_frames.variables()+p_noise.variables()+end_encoder.variables()

            d_loss = l.d_loss
            g_loss = l.g_loss
            metrics = {
                    'g_loss': l.g_loss,
                    'd_loss': l.d_loss
                }

 
            if config.g_random:
                g_noise = self.create_component(config.proxy_noise_generator, input=random_like(z_g_prev.sample), name='g_noise')
               
                if config.style:
                    s_noise = self.create_component(config.proxy_noise_generator, input=random_like(style_next.sample), name='s_noise')
                    g_input = tf.concat(values=[g_noise.sample, z_noise], axis=3)
                    gen = self.create_component(config.generator, features=[s_noise.sample], input=g_input, name='prev_generator', reuse=True)
                else:
                    gen = self.create_component(config.generator, features=[z_noise], input=g_noise.sample, name='prev_generator', reuse=True)
                g_n = tf.slice(gen.sample, [0,0,0,0], [-1,-1,-1,3])
                g_p = tf.slice(gen.sample, [0,0,0,3], [-1,-1,-1,3])
                t0 = target_next
                f0 = target_prev
                t1 = g_n
                f1 = g_p
                stack = [t0, t1]
                stacked = ops.concat(stack, axis=0)
                features = ops.concat([f0, f1], axis=0)
                z_d = self.create_component(config.discriminator, name='g_random', input=stacked, features=[features])
                loss3 = self.create_component(config.loss, discriminator = z_d, x=None, generator=None, split=2)
                metrics["random_g"]=loss3.g_loss*(config.g_random_lambda or 1)
                metrics["random_d"]=loss3.d_loss*(config.g_random_lambda or 1)
                d_loss1 += loss3.d_loss
                g_loss1 += loss3.g_loss
                d_vars1 += z_d.variables()
                g_vars1 += g_noise.variables()
                if config.style:
                    g_vars1 += s_noise.variables()


            if config.alice_map:

                if config.same_g:
                    if config.style:
                        enc_input = tf.concat(self.frames[1:-1], axis=3)
                        frame_enc = self.create_component(config.encoder, input=enc_input, name='prev_encoder', reuse=True)
                        gen_input = tf.concat([frame_enc.sample,random_like(z_noise)],axis=3)
                        if config.skip_connections:
                            gen = self.create_component(config.generator, skip_connections=frame_enc.layers, features=[style_next.sample], input=gen_input, name='prev_generator', reuse=True)
                        else:
                            gen = self.create_component(config.generator, features=[style_next.sample], input=gen_input, name='prev_generator', reuse=True)
                        p_frame = tf.slice(gen.sample, [0,0,0,3],[-1,-1,-1,3])
                        enc_input = tf.concat([p_frame]+self.frames[1:-2], axis=3)
                        frame_enc = self.create_component(config.encoder, input=enc_input, name='prev_encoder', reuse=True)
                        if config.skip_connections:
                            x_hat = self.create_component(config.generator, skip_connections=frame_enc.layers, features=[style_next.sample], input=gen_input, name='prev_generator', reuse=True).sample
                        else:
                            x_hat = self.create_component(config.generator, features=[style_next.sample], input=gen_input, name='prev_generator', reuse=True).sample
                        x_hat = tf.slice(x_hat, [0,0,0,0], [-1,-1,-1,3])



                    else:
                        enc_input = tf.concat(self.frames[1:-1], axis=3)
                        frame_enc = self.create_component(config.encoder, input=enc_input, name='prev_encoder', reuse=True)

                        if config.skip_connections:
                            gen = self.create_component(config.generator, skip_connections=frame_enc.layers, features=[random_like(z_noise)], input=frame_enc.sample, name='prev_generator', reuse=True)
                        else:
                            gen = self.create_component(config.generator, features=[random_like(z_noise)], input=frame_enc.sample, name='prev_generator', reuse=True)
                        p_frame = tf.slice(gen.sample, [0,0,0,3],[-1,-1,-1,3])
                        enc_input = tf.concat([p_frame]+self.frames[1:-2], axis=3)
                        frame_enc = self.create_component(config.encoder, input=enc_input, name='prev_encoder', reuse=True)
                        if config.skip_connections:
                            x_hat = self.create_component(config.generator, skip_connections=frame_enc.layers,features=[random_like(z_noise)], input=frame_enc.sample, name='prev_generator', reuse=True).sample
                        else:
                            x_hat = self.create_component(config.generator, features=[random_like(z_noise)], input=frame_enc.sample, name='prev_generator', reuse=True).sample
                        x_hat = tf.slice(x_hat, [0,0,0,0], [-1,-1,-1,3])


                else:
                    enc_input = tf.concat(self.frames[1:], axis=3)
                    g_frame_encoder = self.create_component(config.encoder, input=enc_input, name='next_encoder', reuse=True)
                    g_frame_input = tf.concat(values=[z_g_prev.sample, z_noise], axis=3)
                    g_frame = self.create_component(config.generator, features=[style_next.sample], input=g_frame_input, name='next_generator', reuse=True)
                    g_frames = self.frames[2:]+[g_frame.sample]

                    g_frame_encoder_input = tf.concat(g_frames, axis=3)
                    g_frame_encoder = self.create_component(config.encoder, input=g_frame_encoder_input, name='prev_encoder', reuse=True)
                    x_hat_input = tf.concat([g_frame_encoder.sample, n_noise], axis=3)
                    x_hat = self.create_component(config.generator, features=[style_prev.sample], input=x_hat_input, name='prev_generator', reuse=True)

            if config.alice_map:
                for term in config.alice_map:
                    t1 = self.frames[-2]
                    t2 = x_hat
                    f1 = self.frames[term]
                    f2 = self.frames[term]
                    stack = [t1, t2]
                    stacked = ops.concat(stack, axis=0)
                    features = ops.concat([f1, f2], axis=0)
                    z_d = self.create_component(config.discriminator, name='alice_discriminator'+str(term), input=stacked, features=[features])
                    loss3 = self.create_component(config.loss, discriminator = z_d, x=None, generator=None, split=2)
                    metrics["alice_gloss_"+str(term)]=loss3.g_loss
                    metrics["alice_dloss_"+str(term)]=loss3.d_loss
                    d_loss1 += loss3.d_loss
                    g_loss1 += loss3.g_loss
                    d_vars1 += z_d.variables()

            if config.align_map:
                 for term in config.align_map:
                    t1 = target_next
                    t2 = gx.sample
                    f1 = self.frames[term]
                    f2 = self.frames[term]
                    stack = [t1, t2]
                    stacked = ops.concat(stack, axis=0)
                    features = ops.concat([f1, f2], axis=0)
                    z_d = self.create_component(config.discriminator, name='alice_discriminator'+str(term), input=stacked, features=[features])
                    loss3 = self.create_component(config.loss, discriminator = z_d, x=None, generator=None, split=2)
                    metrics["align_gloss_"+str(term)]=loss3.g_loss
                    metrics["align_dloss_"+str(term)]=loss3.d_loss
                    d_loss1 += loss3.d_loss
                    g_loss1 += loss3.g_loss
                    d_vars1 += z_d.variables()





            if config.alpha:
                if config.same_g:

                    #proxy_noise = self.create_component(config.proxy_noise_generator, input=random_like(z_g_prev.sample), name='alpha_proxy_noise')
                    #proxy_noise_real = self.create_component(config.proxy_noise_generator, input=z_g_prev.sample, name='alpha_proxy_noise', reuse=True)
                    #t0 = proxy_noise_real.sample
                    #t1 = proxy_noise.sample
                    t0 = random_like(z_g_prev.sample)
                    t1 = z_g_prev.sample
                    netzd = tf.concat(axis=0, values=[t0,t1])
                    z_d = self.create_component(config.z_discriminator, name='z_discriminator', input=netzd)
                    loss3 = self.create_component(config.loss, discriminator = z_d, x=None, generator=None, split=2)
                    metrics["alpha_gloss"]=loss3.g_loss
                    metrics["alpha_dloss"]=loss3.d_loss
                    d_loss1 += loss3.d_loss * (config.alpha_lambda or 1)
                    g_loss1 += loss3.g_loss * (config.alpha_lambda or 1)
                    d_vars1 += z_d.variables()
                    #g_vars1 += proxy_noise.variables()


                else:
                    t0 = random_like(zx)
                    t1 = zx
                    t2 = zy
                    netzd = tf.concat(axis=0, values=[t0,t1,t2])
                    z_d = self.create_component(config.z_discriminator, name='z_discriminator', input=netzd)
                    loss3 = self.create_component(config.loss, discriminator = z_d, x=None, generator=None, split=3)
                    metrics["za_gloss"]=loss3.g_loss
                    metrics["za_dloss"]=loss3.d_loss
                    d_loss1 += loss3.d_loss
                    g_loss1 += loss3.g_loss
                    d_vars1 += z_d.variables()

            trainers = []

            lossa = hc.Config({'sample': [d_loss1, g_loss1], 'metrics': metrics})
            #lossb = hc.Config({'sample': [d_loss2, g_loss2], 'metrics': metrics})
            #trainers += [ConsensusTrainer(self, config.trainer, loss = lossa, g_vars = g_vars1, d_vars = d_vars1)]
            trainer = ConsensusTrainer(self, config.trainer, loss = lossa, g_vars = g_vars1, d_vars = d_vars1)
            #trainer = MultiTrainerTrainer(trainers)
            self.session.run(tf.global_variables_initializer())

        self.trainer = trainer
        self.generator = gx
        self.z_hat = gy.sample
        self.x_input = self.inputs.frames[0]

        self.uga = self.y.sample
        self.uniform_encoder = z_g_prev



    def create_loss(self, loss_config, discriminator, x, generator, split):
        loss = self.create_component(loss_config, discriminator = discriminator, x=x, generator=generator, split=split)
        return loss

    def create_encoder(self, x_input, name='input_encoder'):
        config = self.config
        input_encoder = dict(config.input_encoder or config.g_encoder or config.generator)
        encoder = self.create_component(input_encoder, name=name, input=x_input)
        return encoder

    def create_z_discriminator(self, z, z_hat):
        config = self.config
        z_discriminator = dict(config.z_discriminator or config.discriminator)
        z_discriminator['layer_filter']=None
        net = tf.concat(axis=0, values=[z, z_hat])
        encoder_discriminator = self.create_component(z_discriminator, name='z_discriminator', input=net)
        return encoder_discriminator

    def create_cycloss(self, x_input, x_hat):
        config = self.config
        ops = self.ops
        distance = config.distance or ops.lookup('l1_distance')
        pe_layers = self.gan.skip_connections.get_array("progressive_enhancement")
        cycloss_lambda = config.cycloss_lambda
        if cycloss_lambda is None:
            cycloss_lambda = 10
        
        if(len(pe_layers) > 0):
            mask = self.progressive_growing_mask(len(pe_layers)//2+1)
            cycloss = tf.reduce_mean(distance(mask*x_input,mask*x_hat))

            cycloss *= mask
        else:
            cycloss = tf.reduce_mean(distance(x_input, x_hat))

        cycloss *= cycloss_lambda
        return cycloss


    def create_z_cycloss(self, z, x_hat, encoder, generator):
        config = self.config
        ops = self.ops
        total = None
        distance = config.distance or ops.lookup('l1_distance')
        if config.z_hat_lambda:
            z_hat_cycloss_lambda = config.z_hat_cycloss_lambda
            recode_z_hat = encoder.reuse(x_hat)
            z_hat_cycloss = tf.reduce_mean(distance(z_hat,recode_z_hat))
            z_hat_cycloss *= z_hat_cycloss_lambda
        if config.z_cycloss_lambda:
            recode_z = encoder.reuse(generator.reuse(z))
            z_cycloss = tf.reduce_mean(distance(z,recode_z))
            z_cycloss_lambda = config.z_cycloss_lambda
            if z_cycloss_lambda is None:
                z_cycloss_lambda = 0
            z_cycloss *= z_cycloss_lambda

        if config.z_hat_lambda and config.z_cycloss_lambda:
            total = z_cycloss + z_hat_cycloss
        elif config.z_cycloss_lambda:
            total = z_cycloss
        elif config.z_hat_lambda:
            total = z_hat_cycloss
        return total



    def input_nodes(self):
        "used in hypergan build"
        if hasattr(self.generator, 'mask_generator'):
            extras = [self.mask_generator.sample]
        else:
            extras = []
        return extras + [
                self.x_input
        ]


    def output_nodes(self):
        "used in hypergan build"

    
        if hasattr(self.generator, 'mask_generator'):
            extras = [
                self.mask_generator.sample, 
                self.generator.g1x,
                self.generator.g2x
            ]
        else:
            extras = []
        return extras + [
                self.encoder.sample,
                self.generator.sample, 
                self.uniform_sample,
                self.generator_int
        ]
class VideoFrameSampler(BaseSampler):
    def __init__(self, gan, samples_per_row=8):
        sess = gan.session

        self.c, self.x = gan.session.run([gan.c,gan.x_input])
        self.i = 0
        BaseSampler.__init__(self, gan, samples_per_row)

    def _sample(self):
        gan = self.gan
        z_t = gan.uniform_encoder.sample
        sess = gan.session

        self.c, self.x = sess.run([gan.c_next, gan.video_next], {gan.c: self.c, gan.x_input: self.x})
        v = sess.run(gan.video_sample)
        #next_z, next_frame = sess.run([gan.cz_next, gan.video_sample])

        time.sleep(0.05)
        return {


            'generator': np.hstack([self.x, v])
        }


class TrainingVideoFrameSampler(BaseSampler):
    def __init__(self, gan, samples_per_row=8):
        self.z = None

        self.x_input, self.last_frame_1, self.last_frame_2, self.z1, self.z2 = gan.session.run([gan.x_input, gan.inputs.x1, gan.inputs.x2, gan.z1, gan.z2])

        self.i = 0
        BaseSampler.__init__(self, gan, samples_per_row)

    def _sample(self):
        gan = self.gan
        z_t = gan.uniform_encoder.sample
        sess = gan.session
        
        x_hat,  next_frame, c = sess.run([gan.x_hat, gan.x_next, gan.c], {gan.x_input:self.x_input, gan.last_frame_1:self.last_frame_1, gan.last_frame_2:self.last_frame_2})
        xt1, c = sess.run([gan.x_next, gan.c], {gan.x_input:next_frame, gan.c2:c})
        xt2, c = sess.run([gan.x_next, gan.c], {gan.x_input:next_frame, gan.c2:c})
 
        return {
            'generator': np.vstack([self.last_frame_1, self.last_frame_2, x_hat, next_frame, xt1, xt2])
        }




def setup_gan(config, inputs, args):
    gan = AliNextFrameGAN(config, inputs=inputs)

    if(args.action != 'search' and os.path.isfile(save_file+".meta")):
        gan.load(save_file)

    tf.train.start_queue_runners(sess=gan.session)

    config_name = args.config
    GlobalViewer.title = "[hypergan] next-frame " + config_name
    GlobalViewer.enabled = args.viewer
    GlobalViewer.zoom = args.zoom

    return gan

def train(config, inputs, args):
    gan = setup_gan(config, inputs, args)
    sampler = lookup_sampler(args.sampler or TrainingVideoFrameSampler)(gan)
    samples = 0

    #metrics = [batch_accuracy(gan.inputs.x, gan.uniform_sample), batch_diversity(gan.uniform_sample)]
    #sum_metrics = [0 for metric in metrics]
    for i in range(args.steps):
        gan.step()

        if args.action == 'train' and i % args.save_every == 0 and i > 0:
            print("saving " + save_file)
            gan.save(save_file)

        if i % args.sample_every == 0:
            sample_file="samples/%06d.png" % (samples)
            samples += 1
            sampler.sample(sample_file, args.save_samples)

        #if i > args.steps * 9.0/10:
        #    for k, metric in enumerate(gan.session.run(metrics)):
        #        print("Metric "+str(k)+" "+str(metric))
        #        sum_metrics[k] += metric 

    tf.reset_default_graph()
    return []#sum_metrics

def sample(config, inputs, args):
    gan = setup_gan(config, inputs, args)
    sampler = lookup_sampler(args.sampler or VideoFrameSampler)(gan)
    samples = 0
    for i in range(args.steps):
        sample_file="samples/%06d.png" % (samples)
        samples += 1
        sampler.sample(sample_file, args.save_samples)

def search(config, inputs, args):
    metrics = train(config, inputs, args)

    config_filename = "colorizer-"+str(uuid.uuid4())+'.json'
    hc.Selector().save(config_filename, config)
    with open(args.search_output, "a") as myfile:
        myfile.write(config_filename+","+",".join([str(x) for x in metrics])+"\n")

if args.action == 'train':
    metrics = train(config, inputs, args)
    print("Resulting metrics:", metrics)
elif args.action == 'sample':
    sample(config, inputs, args)
elif args.action == 'search':
    search(config, inputs, args)
else:
    print("Unknown action: "+args.action)