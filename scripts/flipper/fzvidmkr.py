#!/usr/bin/env python3
"""
fzvidmkr - licensed under gplv3
"""
import argparse
import locale
import math
import struct
import subprocess
import sys
import textwrap

from fractions import Fraction
from pathlib import Path

import ffmpeg
import numpy as np

# set locale to user default
locale.setlocale(locale.LC_ALL, '')

# constants
BUNDLE_SIGNATURE = "BND!VID"
BUNDLE_VERSION = 1
SCREEN_WIDTH = 128
SCREEN_HEIGHT = 64

# a class to make sure a valid scale is given in args
class VideoScale:
    def __init__(self, scale):
        [self.width, self.height] = list(map(int, scale.split('x')))
        if not (1 <= self.width <= SCREEN_WIDTH and 1 <= self.height <= SCREEN_HEIGHT):
            raise argparse.ArgumentTypeError(f"{scale} is not in range 1x1 to {SCREEN_WIDTH:d}x{SCREEN_HEIGHT:d}")

    def __str__(self):
        return f'{self.width:d}x{self.height:d}'

# a function to make sure a valid threshold is given in args
def Threshold(t):
    t = int(t)
    if not (0 <= t <= 256):
        raise argparse.ArgumentTypeError(f"{t:d} is not in range 0 to 256")
    return t

# a function to make sure a valid bayer_scale is given in args
def BayerScale(s):
    s = int(s)
    if not (0 <= s <= 5):
        raise argparse.ArgumentTypeError(f"{s:d} is not in range 0 to 5")
    return s

# python uses half round even, but we need to use half round up
# in order to get an accurate frame count, because that's what
# ffmpeg uses when changing frame rates
def half_round_up(fraction):
    if (fraction % 1 < Fraction(1, 2)):
        return math.floor(fraction)
    return math.ceil(fraction)

# setup the argument parser
parser = argparse.ArgumentParser(
        description="A utility to convert videos to a format playable on the F0 (bnd) with animated dithering")

parser_exclusive_1 = parser.add_mutually_exclusive_group()

parser.add_argument('source',
                    type=Path,
                    help="the source file; must contain a video and audio stream")
parser.add_argument('output',
                    type=Path,
                    help="the resulting bundle")
parser_exclusive_1.add_argument('-d', '--dither',
                    choices=["bayer",
                             "heckbert",
                             "floyd_steinberg",
                             "sierra2",
                             "sierra2_4a",
                             "sierra3",
                             "burkes",
                             "atkinson",
                             "none"],
                    default="sierra3",
                    metavar='ALGORITHM',
                    help="the dithering algorithm to use, or 'none' to disable; for a list of options, see FFmpeg's 'paletteuse'; defaults to 'sierra3'")
parser.add_argument('--bayer-scale',
                    type=BayerScale,
                    dest='bayer_scale',
                    help="used with '-d/--dither bayer' to define the scale of the pattern (how much crosshatch is visible), from 0 to 5; defaults to '2'")
parser_exclusive_1.add_argument('-t', '--threshold',
                    type=Threshold,
                    help="the threshold to apply when converting to black and white, from 0 to 256; cannot be used with dithering")
parser.add_argument('-f', '--frame-rate',
                    type=Fraction,
                    dest='frame_rate',
                    help="the desired video frame rate, may be a fraction; defaults to source frame rate")
parser.add_argument('-s', '--scale',
                    type=VideoScale,
                    dest='scale',
                    help=f"the desired video size, cannot be larger than {SCREEN_WIDTH:d}x{SCREEN_HEIGHT:d}; default best fit")
parser.add_argument('-r', '--sample-rate',
                    type=int,
                    dest='sample_rate',
                    help="the desired audio sample rate; defaults to the source sample rate")
parser.add_argument('-a', '--animate-dither',
                    action='store_true',
                    default=True,  
                    dest='animate_dither',
                    help="enable animated dithering that shifts the pattern by one pixel each frame to reduce LCD flickering and emulate opacity (default)")
parser.add_argument('--no-animate-dither',
                    action='store_false',
                    dest='animate_dither',
                    help="disable animated dithering to use traditional static dithering")
parser.add_argument('-q', '--quiet',
                    action='count',
                    default=0,
                    help="don't output info to stdout, use twice to silence warnings")

args = parser.parse_args()

# only allow '--bayer-scale` to be used with bayer dithering
if args.bayer_scale != None and args.dither != 'bayer':
    parser.error("--bayer-scale can only be used with '-d/--dither bayer'")

# get media information
video_index = None
audio_index = None
ffprobe_result = ffmpeg.probe(args.source, count_packets=None)
for stream in ffprobe_result['streams']:
    if stream['disposition']['default']:
        if stream['codec_type'] == 'video' and video_index == None:
            source_frame_count = int(stream['nb_read_packets'])
            source_width = int(stream['width'])
            source_height = int(stream['height'])
            source_frame_rate = Fraction(stream['r_frame_rate'])
            video_index = stream['index']
        if stream['codec_type'] == 'audio' and audio_index == None:
            source_sample_rate = int(stream['sample_rate'])
            audio_index = stream['index']

# display an error if video or audio is missing
if video_index == None:
    parser.error("source file does not contain a default video stream")
if audio_index == None:
    parser.error("source file does not contain a default audio stream")

# get the video dimensions before padding
if args.scale == None:
    # default: maintain aspect ratio and scale to fit screen
    scale_factor = max(source_width / SCREEN_WIDTH, source_height / SCREEN_HEIGHT)
    pre_pad_width = math.floor(source_width / scale_factor)
    frame_height = math.floor(source_height / scale_factor)
else:
    # user defined dimensions
    pre_pad_width = args.scale.width
    frame_height = args.scale.height

# get width after padding and final frame size
frame_width = pre_pad_width + 8 - (pre_pad_width % 8)
frame_size = int(frame_width * frame_height / 8)

# determine sample and frame rates
sample_rate = args.sample_rate or source_sample_rate
frame_rate = args.frame_rate or source_frame_rate

# calculate new frame count
frame_count = half_round_up(source_frame_count * frame_rate / source_frame_rate)

# calculate audio chunk size
audio_chunk_size = (source_frame_count * sample_rate) / (source_frame_rate * frame_count)

# used to calculate which samples to drop to prevent desync
audio_sample_drop_rate = audio_chunk_size % 1
audio_chunk_size = int(audio_chunk_size)

# estimate the final size, used later to check for errors
estimated_file_size = int(
        (((frame_width * frame_height) / 8) + audio_chunk_size)
        * frame_count + len(BUNDLE_SIGNATURE) + 11)

# print final bundle info
if args.quiet < 1:
    print(textwrap.dedent(f'''\
        Frame rate:                   {float(frame_rate):g} fps
        Frame count:                  {frame_count:d} frames
        Video scale (before padding): {pre_pad_width:d}x{frame_height:d}
        Video scale (after padding):  {frame_width:d}x{frame_height:d}
        Audio sample rate:            {sample_rate:d} Hz
        Audio chunk size:             {audio_chunk_size:d} bytes
        Estimated file size:          {estimated_file_size:n} bytes
        '''))

    if frame_count > source_frame_count:
        print(f"{frame_count - source_frame_count:d} frames will be duplicated\n")

    if frame_count < source_frame_count:
        print(f"{source_frame_count - frame_count:d} frames will be dropped\n")

if args.quiet < 2:
    if frame_rate > 30:
        print("warning: frame rate is greater than maximum recommended 30 fps\n")

    if sample_rate > 48000:
        print("warning: sample rate is greater than maximum recommended 48 kHz\n")

# open the output file for writing, ensuring .bnd extension
if f"{args.output}".endswith(".bnd"):
    output = open(args.output, 'wb')
else:
    output = open(f"{args.output}.bnd", 'wb')

# specify the input file
input = ffmpeg.input(args.source)

audio_process = (
        input[str(audio_index)]
        # output raw 8-bit audio
        .output('pipe:',
                format='u8',
                acodec='pcm_u8',
                ac=1,
                ar=sample_rate)
        # only display errors
        .global_args('-v', 'error')

        .run_async(pipe_stdout=True)
)

# process video with or without animated dithering (animated dithering is now default)
if args.animate_dither:
    # for animated dithering, we need to process frames individually
    # first, get the grayscale video without dithering
    scaled_video = (
        input[str(video_index)]
        # scale the video
        .filter('scale', pre_pad_width, frame_height)
        # convert to grayscale
        .filter('format', 'gray')
        # set the frame rate
        .filter('fps', frame_rate)
    )
    
    # process without dithering initially, we'll apply animated dithering later
    video_process = (
        scaled_video
        # pad the width to make sure it is a multiple of 8
        .filter('pad', frame_width, frame_height, -1, 0, 'white')
        # output raw video data in grayscale format for further processing
        .output('pipe:',
                format='rawvideo',
                pix_fmt='gray')
        # only display errors
        .global_args('-v', 'error')

        .run_async(pipe_stdout=True)
    )
else:
    # static dithering
    scaled_video = (
        input[str(video_index)]
        # scale the video
        .filter('scale', pre_pad_width, frame_height)
        # convert to grayscale
        .filter('format', 'gray')
        # set the frame rate
        .filter('fps', frame_rate)
    )

    if args.threshold != None:
        # convert to black and white with threshold
        video_input = scaled_video.filter('maskfun',
                                        low=args.threshold - 1,
                                        high=args.threshold - 1,
                                        sum=256,
                                        fill=255)
    else:
        # the palette used for dithering
        palette = ffmpeg.filter([
            ffmpeg.input('color=c=black:r=1:d=1:s=8x16', f='lavfi'),
            ffmpeg.input('color=c=white:r=1:d=1:s=8x16', f='lavfi')
        ], 'hstack', 2)

        # convert to black and white with dithering
        if (args.dither == 'bayer' and args.bayer_scale != None):
            # if a bayer_scale was provided
            video_input = ffmpeg.filter([scaled_video, palette],
                                        'paletteuse',
                                        new='true',
                                        dither=args.dither,
                                        bayer_scale=args.bayer_scale)
        else:
            video_input = ffmpeg.filter([scaled_video, palette],
                                        'paletteuse',
                                        new='true',
                                        dither=args.dither)

    video_process = (
        video_input
        # pad the width to make sure it is a multiple of 8
        .filter('pad', frame_width, frame_height, -1, 0, 'white')
        # output raw video data, one bit per pixel, inverted, and
        # disable dithering (we've already handled it)
        .output('pipe:',
                sws_dither='none',
                format='rawvideo',
                pix_fmt='monow')
        # only display errors
        .global_args('-v', 'error')

        .run_async(pipe_stdout=True)
    )

# header format:
#  signature (char[7] / 7s): "BND!VID"
#  version (uint8 / B): 1
#  frame_count (uint32 / I)
#  audio_chunk_size (uint16 / H): sample_rate / frame_rate
#  sample_rate (uint16 / H)
#  frame_height (uint8 / B)
#  frame_width (uint8 / B)
header = struct.pack(f'<{len(BUNDLE_SIGNATURE):d}sBIHHBB',
                     BUNDLE_SIGNATURE.encode('utf8'),
                     BUNDLE_VERSION,
                     frame_count,
                     audio_chunk_size,
                     sample_rate,
                     frame_height,
                     frame_width)

# write the header to the file
output.write(header)
bytes_written = len(header)

# the number of audio samples that need to be dropped
drop_samples = audio_sample_drop_rate
dropped_samples = 0

for frame_num in range(1, frame_count + 1):
    # print current progress every 10 seconds of video
    if args.quiet < 1 and (
            frame_num % math.floor(frame_rate * 10) == 0 or
            frame_num == 1 or
            frame_num == frame_count):
        print(f"Processing frame {frame_num:>{len(str(frame_count))}d} / {frame_count:d}: {frame_num / frame_count:>7.2%}")

    # read a single frame and audio chunk
    if args.animate_dither:
        # Read grayscale frame data
        frame_raw = video_process.stdout.read(frame_width * frame_height)
        if not frame_raw:
            break
        
        # convert to numpy array
        gray_frame = np.frombuffer(frame_raw, dtype=np.uint8).reshape((frame_height, frame_width))
        
        # apply animated dithering - shift the dithering pattern by frame number
        # create a bayer matrix for ordered dithering
        bayer_matrix = np.array([[0, 8, 2, 10],
                                [12, 4, 14, 6],
                                [3, 11, 1, 9],
                                [15, 7, 13, 5]]) / 16.0
        
        # shift the dithering pattern based on the current frame
        shift_x = frame_num % 4  # cycle through 4 positions horizontally
        shift_y = (frame_num // 4) % 4  # cycle through 4 positions vertically
        
        # apply the shifted dithering pattern
        binary_frame = np.zeros_like(gray_frame, dtype=np.uint8)
        for y in range(frame_height):
            for x in range(frame_width):
                # calculate the dithering threshold with shifting
                bayer_x = (x + shift_x) % 4
                bayer_y = (y + shift_y) % 4
                threshold = bayer_matrix[bayer_y, bayer_x] * 255
                
                # dithering
                if gray_frame[y, x] > threshold:
                    binary_frame[y, x] = 255
                else:
                    binary_frame[y, x] = 0
        
        # convert to the monochrome 1-bit format
        frame_data = bytearray()
        for y in range(frame_height):
            for x_byte in range(0, frame_width, 8):
                byte_val = 0
                for bit in range(8):
                    px = x_byte + bit
                    if px < frame_width:
                        # flip the bit!
                        if binary_frame[y, px] == 0:
                            byte_val |= (1 << bit)
                frame_data.append(byte_val)
    else:
        # without animated dithering
        frame = video_process.stdout.read(frame_size)
        if not frame:
            break
            
        # reverse the bit-order of each byte in the frame
        frame_data = bytearray()
        for byte in frame:
            frame_data.append(int(f'{byte:08b}'[::-1], 2))

    # read audio chunk
    audio_chunk = audio_process.stdout.read(audio_chunk_size)

    # calculate and drop samples; prevents desync
    drop_samples += audio_sample_drop_rate
    audio_process.stdout.read(int(drop_samples))
    dropped_samples += int(drop_samples)
    drop_samples %= 1

    # write frame and audio data
    output.write(frame_data)
    output.write(audio_chunk)
    bytes_written += len(frame_data) + len(audio_chunk)

# close the file descriptor
output.close()

# wait for ffmpeg processes to finish
video_process.wait()
audio_process.wait()

if args.quiet < 1:
    print()

    if dropped_samples > 0:
        print(f"{dropped_samples:n} audio samples were dropped to prevent desync\n")

    print(f"{bytes_written:n} bytes written to {args.output}\n")

if args.quiet < 2:
    if bytes_written != estimated_file_size:
        print(f"warning: number of bytes written does not match estimated file size, something may have gone wrong\n")