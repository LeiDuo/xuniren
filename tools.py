# python nerf/asr.py --wav ../data/audio/aud.wav --save_feats
import argparse
import subprocess
from queue import Queue, Full, Empty
from threading import Thread, Event

import librosa
import pyaudio
import resampy
import soundfile as sf
from transformers import AutoModelForCTC, AutoProcessor

from nerf.network import NeRFNetwork
from nerf.provider import NeRFDataset_Test
from nerf.utils import *

fps = 25
width = 512
height = 512
frame_sec = 1 / fps
push_url = "rtsp://127.0.0.1:8554/humanlive"
video_pipe_name = "video_pipe"
audio_pipe_name = "audio_pipe"
command = [
    "ffmpeg",
    "-loglevel",
    "info",
    "-y",
    "-an",
    "-f",
    "rawvideo",
    "-vcodec",
    "rawvideo",
    "-pix_fmt",
    "rgb24",
    "-s",
    "{}x{}".format(width, height),
    "-r",
    str(fps),
    "-re",
    "-i",
    video_pipe_name,  # 视频流管道作为输入
    "-f",
    "s16le",
    "-acodec",
    "pcm_s16le",
    "-i",
    audio_pipe_name,  # 音频流管道作为输入
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-s",
    "512x512",
    "-r",
    str(fps),
    "-preset",
    "ultrafast",
    "-profile:v",
    "baseline",
    "-tune",
    "zerolatency",
    "-g",
    "2",
    "-b:v",
    "1000k",
    "-ac",
    "1",
    "-ar",
    "44100",
    "-acodec",
    "aac",
    "-shortest",
    "-f",
    "rtsp",
    push_url,
]
fd_v = None
fd_a = None
v_access = True
sync_q = Queue(1)
write_v_f = 0
write_a_f = 0
v_full_idle = imageio.v2.imread("./data/video/idle_pic.png")
a_full_idle = torch.full((int(44100 / fps),), 0).detach().cpu().numpy().astype(np.int16)


def _read_frame(stream, exit_event, queue, chunk):
    while True:
        if exit_event.is_set():
            print(f"[INFO] read frame thread ends")
            break
        frame = stream.read(chunk, exception_on_overflow=False)
        frame = (
                np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32767
        )  # [chunk]
        queue.put(frame)


def _play_frame(stream, exit_event, queue, chunk):
    while True:
        if exit_event.is_set():
            print(f"[INFO] play frame thread ends")
            break
        frame = queue.get()
        frame = (frame * 32767).astype(np.int16).tobytes()
        stream.write(frame, chunk)


class ASR:
    def __init__(self, opt, processor, loadmodel, asr_wav):

        self.opt = opt

        self.play = False
        self.asr_wav = asr_wav
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.fps = opt.fps  # 20 ms per frame
        self.sample_rate = 16000
        self.chunk = (
                self.sample_rate // self.fps
        )  # 320 samples per chunk (20ms * 16000 / 1000)
        self.mode = "live" if self.asr_wav == "" else "file"

        if "esperanto" in self.opt.asr_model:
            self.audio_dim = 44
        elif "deepspeech" in self.opt.asr_model:
            self.audio_dim = 29
        else:
            self.audio_dim = 32

        # prepare context cache
        # each segment is (stride_left + ctx + stride_right) * 20ms, latency should be (ctx + stride_right) * 20ms
        self.context_size = opt.m
        self.stride_left_size = opt.l
        self.stride_right_size = opt.r
        self.text = "[START]\n"
        self.terminated = False
        self.frames = []

        # pad left frames
        if self.stride_left_size > 0:
            self.frames.extend(
                [np.zeros(self.chunk, dtype=np.float32)] * self.stride_left_size
            )

        self.exit_event = Event()
        self.audio_instance = pyaudio.PyAudio()

        # create input stream
        if self.mode == "file":
            self.file_stream = self.create_file_stream()
        else:
            # start a background process to read frames
            self.input_stream = self.audio_instance.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                output=False,
                frames_per_buffer=self.chunk,
            )
            self.queue = Queue()
            self.process_read_frame = Thread(
                target=_read_frame,
                args=(self.input_stream, self.exit_event, self.queue, self.chunk),
            )

        # play out the audio too...?
        if self.play:
            self.output_stream = self.audio_instance.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=False,
                output=True,
                frames_per_buffer=self.chunk,
            )
            self.output_queue = Queue()
            self.process_play_frame = Thread(
                target=_play_frame,
                args=(
                    self.output_stream,
                    self.exit_event,
                    self.output_queue,
                    self.chunk,
                ),
            )

        # current location of audio
        self.idx = 0
        """
            模型加载

        # create wav2vec model
        print(f'[INFO] loading ASR model {self.opt.asr_model}...')
        self.processor = AutoProcessor.from_pretrained(opt.asr_model)
        self.model = AutoModelForCTC.from_pretrained(opt.asr_model).to(self.device)
        """
        self.processor = processor
        self.model = loadmodel
        # prepare to save logits
        if self.opt.asr_save_feats:
            self.all_feats = []

        # the extracted features
        # use a loop queue to efficiently record endless features: [f--t---][-------][-------]
        self.feat_buffer_size = 4
        self.feat_buffer_idx = 0
        self.feat_queue = torch.zeros(
            self.feat_buffer_size * self.context_size,
            self.audio_dim,
            dtype=torch.float32,
            device=self.device,
        )

        # TODO: hard coded 16 and 8 window size...
        self.front = self.feat_buffer_size * self.context_size - 8  # fake padding
        self.tail = 8
        # attention window...
        self.att_feats = [
                             torch.zeros(self.audio_dim, 16, dtype=torch.float32, device=self.device)
                         ] * 4  # 4 zero padding...

        # warm up steps needed: mid + right + window_size + attention_size
        self.warm_up_steps = self.context_size + self.stride_right_size + 8 + 2 * 3

        self.listening = False
        self.playing = False

    def listen(self):
        # start
        if self.mode == "live" and not self.listening:
            print(f"[INFO] starting read frame thread...")
            self.process_read_frame.start()
            self.listening = True

        if self.play and not self.playing:
            print(f"[INFO] starting play frame thread...")
            self.process_play_frame.start()
            self.playing = True

    def stop(self):

        self.exit_event.set()

        if self.play:
            self.output_stream.stop_stream()
            self.output_stream.close()
            if self.playing:
                self.process_play_frame.join()
                self.playing = False

        if self.mode == "live":
            self.input_stream.stop_stream()
            self.input_stream.close()
            if self.listening:
                self.process_read_frame.join()
                self.listening = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):

        self.stop()

        if self.mode == "live":
            # live mode: also print the result text.
            self.text += "\n[END]"
            print(self.text)

    def get_next_feat(self):
        # return a [1/8, 16] window, for the next input to nerf side.

        while len(self.att_feats) < 8:
            # [------f+++t-----]
            if self.front < self.tail:
                feat = self.feat_queue[self.front: self.tail]
            # [++t-----------f+]
            else:
                feat = torch.cat(
                    [self.feat_queue[self.front:], self.feat_queue[: self.tail]], dim=0
                )

            self.front = (self.front + 2) % self.feat_queue.shape[0]
            self.tail = (self.tail + 2) % self.feat_queue.shape[0]

            # print(self.front, self.tail, feat.shape)

            self.att_feats.append(feat.permute(1, 0))

        att_feat = torch.stack(self.att_feats, dim=0)  # [8, 44, 16]

        # discard old
        self.att_feats = self.att_feats[1:]

        return att_feat

    def run_step(self):

        if self.terminated:
            return

        # get a frame of audio
        frame = self.get_audio_frame()

        # the last frame
        if frame is None:
            # terminate, but always run the network for the left frames
            self.terminated = True
        else:
            self.frames.append(frame)
            # put to output
            if self.play:
                self.output_queue.put(frame)
            # context not enough, do not run network.
            if (
                    len(self.frames)
                    < self.stride_left_size + self.context_size + self.stride_right_size
            ):
                return

        inputs = np.concatenate(self.frames)  # [N * chunk]

        # discard the old part to save memory
        if not self.terminated:
            self.frames = self.frames[
                          -(self.stride_left_size + self.stride_right_size):
                          ]

        logits, labels, text = self.frame_to_text(inputs)
        feats = logits  # better lips-sync than labels

        # save feats
        if self.opt.asr_save_feats:
            self.all_feats.append(feats)

        # record the feats efficiently.. (no concat, constant memory)
        start = self.feat_buffer_idx * self.context_size
        end = start + feats.shape[0]
        self.feat_queue[start:end] = feats
        self.feat_buffer_idx = (self.feat_buffer_idx + 1) % self.feat_buffer_size

        # very naive, just concat the text output.
        if text != "":
            self.text = self.text + " " + text

        # will only run once at ternimation
        if self.terminated:
            self.text += "\n[END]"
            print(self.text)
            if self.opt.asr_save_feats:
                print(f"[INFO] save all feats for training purpose... ")
                feats = torch.cat(self.all_feats, dim=0)  # [N, C]
                # print('[INFO] before unfold', feats.shape)
                window_size = 16
                padding = window_size // 2
                feats = (
                    feats.view(-1, self.audio_dim).permute(1, 0).contiguous()
                )  # [C, M]
                feats = feats.view(1, self.audio_dim, -1, 1)  # [1, C, M, 1]
                unfold_feats = F.unfold(
                    feats,
                    kernel_size=(window_size, 1),
                    padding=(padding, 0),
                    stride=(2, 1),
                )  # [1, C * window_size, M / 2 + 1]
                unfold_feats = (
                    unfold_feats.view(self.audio_dim, window_size, -1)
                    .permute(2, 1, 0)
                    .contiguous()
                )  # [C, window_size, M / 2 + 1] --> [M / 2 + 1, window_size, C]
                # print('[INFO] after unfold', unfold_feats.shape)
                # save to a npy file
                if "esperanto" in self.opt.asr_model:
                    output_path = self.asr_wav.replace(".wav", "_eo.npy")
                else:
                    output_path = self.asr_wav.replace(".wav", ".npy")
                np.save(output_path, unfold_feats.cpu().numpy())
                print(f"[INFO] saved logits to {output_path}")

    def create_file_stream(self):

        stream, sample_rate = sf.read(self.asr_wav)  # [T*sample_rate,] float64
        stream = stream.astype(np.float32)

        if stream.ndim > 1:
            print(f"[WARN] audio has {stream.shape[1]} channels, only use the first.")
            stream = stream[:, 0]

        if sample_rate != self.sample_rate:
            print(
                f"[WARN] audio sample rate is {sample_rate}, resampling into {self.sample_rate}."
            )
            stream = resampy.resample(
                x=stream, sr_orig=sample_rate, sr_new=self.sample_rate
            )

        print(f"[INFO] loaded audio stream {self.asr_wav}: {stream.shape}")

        return stream

    def create_pyaudio_stream(self):

        import pyaudio

        print(f"[INFO] creating live audio stream ...")

        audio = pyaudio.PyAudio()

        # get devices
        info = audio.get_host_api_info_by_index(0)
        n_devices = info.get("deviceCount")

        for i in range(0, n_devices):
            if (
                    audio.get_device_info_by_host_api_device_index(0, i).get(
                        "maxInputChannels"
                    )
            ) > 0:
                name = audio.get_device_info_by_host_api_device_index(0, i).get("name")
                print(f"[INFO] choose audio device {name}, id {i}")
                break

        # get stream
        stream = audio.open(
            input_device_index=i,
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=self.chunk,
        )

        return audio, stream

    def get_audio_frame(self):

        if self.mode == "file":

            if self.idx < self.file_stream.shape[0]:
                frame = self.file_stream[self.idx: self.idx + self.chunk]
                self.idx = self.idx + self.chunk
                return frame
            else:
                return None

        else:

            frame = self.queue.get()
            # print(f'[INFO] get frame {frame.shape}')

            self.idx = self.idx + self.chunk

            return frame

    def frame_to_text(self, frame):
        # frame: [N * 320], N = (context_size + 2 * stride_size)

        inputs = self.processor(
            frame, sampling_rate=self.sample_rate, return_tensors="pt", padding=True
        )

        with torch.no_grad():
            result = self.model(inputs.input_values.to(self.device))
            logits = result.logits  # [1, N - 1, 32]

        # cut off stride
        left = max(0, self.stride_left_size)
        right = min(
            logits.shape[1], logits.shape[1] - self.stride_right_size + 1
        )  # +1 to make sure output is the same length as input.

        # do not cut right if terminated.
        if self.terminated:
            right = logits.shape[1]

        logits = logits[:, left:right]

        # print(frame.shape, inputs.input_values.shape, logits.shape)

        predicted_ids = torch.argmax(logits, dim=-1)
        transcription = self.processor.batch_decode(predicted_ids)[0].lower()

        # for esperanto
        # labels = np.array(['ŭ', '»', 'c', 'ĵ', 'ñ', '”', '„', '“', 'ǔ', 'o', 'ĝ', 'm', 'k', 'd', 'a', 'ŝ', 'z', 'i', '«', '—', '‘', 'ĥ', 'f', 'y', 'h', 'j', '|', 'r', 'u', 'ĉ', 's', '–', 'ﬁ', 'l', 'p', '’', 'g', 'v', 't', 'b', 'n', 'e', '[UNK]', '[PAD]'])

        # labels = np.array([' ', ' ', ' ', '-', '|', 'E', 'T', 'A', 'O', 'N', 'I', 'H', 'S', 'R', 'D', 'L', 'U', 'M', 'W', 'C', 'F', 'G', 'Y', 'P', 'B', 'V', 'K', "'", 'X', 'J', 'Q', 'Z'])
        # print(''.join(labels[predicted_ids[0].detach().cpu().long().numpy()]))
        # print(predicted_ids[0])
        # print(transcription)

        return logits[0], predicted_ids[0], transcription  # [N,]

    def run(self):

        self.listen()

        while not self.terminated:
            self.run_step()

    def clear_queue(self):
        # clear the queue, to reduce potential latency...
        print(f"[INFO] clear queue")
        if self.mode == "live":
            self.queue.queue.clear()
        if self.play:
            self.output_queue.queue.clear()

    def warm_up(self):

        self.listen()

        print(
            f"[INFO] warm up ASR live model, expected latency = {self.warm_up_steps / self.fps:.6f}s"
        )
        t = time.time()
        for _ in range(self.warm_up_steps):
            self.run_step()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        t = time.time() - t
        print(f"[INFO] warm-up done, actual latency = {t:.6f}s")

        self.clear_queue()


def audio_pre_process():
    global opt_au, model_au, processor_au
    parser = argparse.ArgumentParser()
    parser.add_argument("--play", action="store_true", help="play out the audio")
    parser.add_argument(
        "--model", type=str, default="cpierse/wav2vec2-large-xlsr-53-esperanto"
    )
    # parser.add_argument('--model', type=str, default='facebook/wav2vec2-large-960h-lv60-self')
    parser.add_argument("--save_feats", default=True, action="store_true")
    # audio FPS
    parser.add_argument("--fps", type=int, default=50)
    # sliding window left-middle-right length.
    parser.add_argument("-l", type=int, default=10)
    parser.add_argument("-m", type=int, default=50)
    parser.add_argument("-r", type=int, default=10)

    opt = parser.parse_args()
    # fix

    # opt.asr_play = opt.play
    opt.asr_model = opt.model
    opt.asr_save_feats = opt.save_feats
    # create wav2vec model
    asr_model = "cpierse/wav2vec2-large-xlsr-53-esperanto"

    print(f"[INFO] loading ASR model {asr_model}...")
    processor_au = AutoProcessor.from_pretrained(asr_model)
    model_au = AutoModelForCTC.from_pretrained(asr_model).to("cuda")
    opt_au = opt


def video_pre_process():
    global opt_vid, model_vid, trainer_vid
    parser = argparse.ArgumentParser()
    # parser.add_argument('--pose', type=str, default="data/kh.json", help="transforms.json, pose source")
    parser.add_argument(
        "--pose", type=str, default="data/kf.json", help="transforms.json, pose source"
    )
    parser.add_argument(
        "--bg_img",
        type=str,
        default="data/bg.jpg",
        help="bg.jpg, background image source",
    )

    parser.add_argument(
        "-O", action="store_true", help="equals --fp16 --cuda_ray --exp_eye"
    )
    # parser.add_argument('--test', action='store_true', help="test mode (load model and test dataset)")
    # parser.add_argument('--test_train', action='store_true', help="test mode (load model and train dataset)")
    parser.add_argument(
        "--data_range", type=int, nargs="*", default=[0, -1], help="data range to use"
    )
    parser.add_argument("--workspace", type=str, default="data/video")
    parser.add_argument("--seed", type=int, default=0)

    ### training options
    # parser.add_argument('--iters', type=int, default=200000, help="training iters")
    # parser.add_argument('--lr', type=float, default=5e-3, help="initial learning rate")
    # parser.add_argument('--lr_net', type=float, default=5e-4, help="initial learning rate")
    # parser.add_argument('--ckpt', type=str, default='data/pretrained/ngp_kh.pth')
    parser.add_argument("--ckpt", type=str, default="data/pretrained/ngp_kf.pth")
    parser.add_argument(
        "--num_rays",
        type=int,
        default=4096 * 16,
        help="num rays sampled per image for each training step",
    )
    parser.add_argument(
        "--cuda_ray",
        action="store_true",
        help="use CUDA raymarching instead of pytorch",
    )
    parser.add_argument(
        "--max_steps",
        type=int,
        default=16,
        help="max num steps sampled per ray (only valid when using --cuda_ray)",
    )
    parser.add_argument(
        "--num_steps",
        type=int,
        default=16,
        help="num steps sampled per ray (only valid when NOT using --cuda_ray)",
    )
    parser.add_argument(
        "--upsample_steps",
        type=int,
        default=0,
        help="num steps up-sampled per ray (only valid when NOT using --cuda_ray)",
    )
    parser.add_argument(
        "--update_extra_interval",
        type=int,
        default=16,
        help="iter interval to update extra status (only valid when using --cuda_ray)",
    )
    parser.add_argument(
        "--max_ray_batch",
        type=int,
        default=4096,
        help="batch size of rays at inference to avoid OOM (only valid when NOT using --cuda_ray)",
    )

    ### network backbone options
    parser.add_argument(
        "--fp16", action="store_true", help="use amp mixed precision training"
    )

    parser.add_argument(
        "--lambda_amb", type=float, default=0.1, help="lambda for ambient loss"
    )

    parser.add_argument("--fbg", action="store_true", help="frame-wise bg")
    parser.add_argument(
        "--exp_eye", action="store_true", help="explicitly control the eyes"
    )
    parser.add_argument(
        "--fix_eye",
        type=float,
        default=-1,
        help="fixed eye area, negative to disable, set to 0-0.3 for a reasonable eye",
    )
    parser.add_argument(
        "--smooth_eye", action="store_true", help="smooth the eye area sequence"
    )

    parser.add_argument(
        "--torso_shrink",
        type=float,
        default=0.8,
        help="shrink bg coords to allow more flexibility in deform",
    )

    ### dataset options
    parser.add_argument(
        "--color_space",
        type=str,
        default="srgb",
        help="Color space, supports (linear, srgb)",
    )
    # parser.add_argument('--preload', action='store_true', help="preload all data into GPU, accelerate training but use more GPU memory")
    # (the default value is for the fox dataset)
    parser.add_argument(
        "--bound",
        type=float,
        default=1,
        help="assume the scene is bounded in box[-bound, bound]^3, if > 1, will invoke adaptive ray marching.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=4,
        help="scale camera location into box[-bound, bound]^3",
    )
    parser.add_argument(
        "--offset",
        type=float,
        nargs="*",
        default=[0, 0, 0],
        help="offset of camera location",
    )
    parser.add_argument(
        "--dt_gamma",
        type=float,
        default=1 / 256,
        help="dt_gamma (>=0) for adaptive ray marching. set to 0 to disable, >0 to accelerate rendering (but usually with worse quality)",
    )
    parser.add_argument(
        "--min_near", type=float, default=0.05, help="minimum near distance for camera"
    )
    parser.add_argument(
        "--density_thresh",
        type=float,
        default=10,
        help="threshold for density grid to be occupied (sigma)",
    )
    parser.add_argument(
        "--density_thresh_torso",
        type=float,
        default=0.01,
        help="threshold for density grid to be occupied (alpha)",
    )
    parser.add_argument(
        "--patch_size",
        type=int,
        default=1,
        help="[experimental] render patches in training, so as to apply LPIPS loss. 1 means disabled, use [64, 32, 16] to enable",
    )

    parser.add_argument(
        "--finetune_lips",
        action="store_true",
        help="use LPIPS and landmarks to fine tune lips region",
    )
    parser.add_argument(
        "--smooth_lips",
        action="store_true",
        help="smooth the enc_a in a exponential decay way...",
    )

    parser.add_argument("--torso", action="store_true", help="fix head and train torso")
    parser.add_argument("--head_ckpt", type=str, default="", help="head model")

    ### GUI options
    parser.add_argument("--gui", action="store_true", help="start a GUI")
    parser.add_argument("--W", type=int, default=450, help="GUI width")
    parser.add_argument("--H", type=int, default=450, help="GUI height")
    parser.add_argument(
        "--radius",
        type=float,
        default=3.35,
        help="default GUI camera radius from center",
    )
    parser.add_argument(
        "--fovy", type=float, default=21.24, help="default GUI camera fovy"
    )
    parser.add_argument(
        "--max_spp", type=int, default=1, help="GUI rendering max sample per pixel"
    )

    ### else
    parser.add_argument(
        "--att",
        type=int,
        default=2,
        help="audio attention mode (0 = turn off, 1 = left-direction, 2 = bi-direction)",
    )
    parser.add_argument(
        "--emb",
        action="store_true",
        help="use audio class + embedding instead of logits",
    )

    parser.add_argument(
        "--ind_dim", type=int, default=4, help="individual code dim, 0 to turn off"
    )
    parser.add_argument(
        "--ind_num",
        type=int,
        default=10000,
        help="number of individual codes, should be larger than training dataset size",
    )

    parser.add_argument(
        "--ind_dim_torso",
        type=int,
        default=8,
        help="individual code dim, 0 to turn off",
    )

    parser.add_argument("--amb_dim", type=int, default=2, help="ambient dimension")
    parser.add_argument(
        "--part", action="store_true", help="use partial training data (1/10)"
    )
    parser.add_argument(
        "--part2", action="store_true", help="use partial training data (first 15s)"
    )

    parser.add_argument(
        "--train_camera", action="store_true", help="optimize camera pose"
    )
    parser.add_argument(
        "--smooth_path",
        action="store_true",
        help="brute-force smooth camera pose trajectory with a window size",
    )
    parser.add_argument(
        "--smooth_path_window", type=int, default=7, help="smoothing window size"
    )

    # asr
    parser.add_argument("--asr", action="store_true", help="load asr for real-time app")
    parser.add_argument(
        "--asr_wav", type=str, default="", help="load the wav and use as input"
    )
    parser.add_argument("--asr_play", action="store_true", help="play out the audio")

    parser.add_argument(
        "--asr_model", type=str, default="cpierse/wav2vec2-large-xlsr-53-esperanto"
    )
    # parser.add_argument('--asr_model', type=str, default='facebook/wav2vec2-large-960h-lv60-self')

    parser.add_argument("--asr_save_feats", action="store_true")
    # audio FPS
    parser.add_argument("--fps", type=int, default=50)
    # sliding window left-middle-right length (unit: 20ms)
    parser.add_argument("-l", type=int, default=10)
    parser.add_argument("-m", type=int, default=50)
    parser.add_argument("-r", type=int, default=10)

    opt = parser.parse_args()

    # assert test mode
    opt.test = True
    opt.test_train = False

    # explicit smoothing
    opt.smooth_path = True
    opt.smooth_eye = True
    opt.smooth_lips = True

    assert opt.pose != "", "Must provide a pose source"

    # if opt.O:
    opt.fp16 = True
    opt.exp_eye = True

    opt.cuda_ray = True
    opt.torso = True
    # assert opt.cuda_ray, "Only support CUDA ray mode."
    seed_everything(opt.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = NeRFNetwork(opt)

    trainer = Trainer(
        "ngp",
        opt,
        model,
        device=device,
        workspace=opt.workspace,
        fp16=opt.fp16,
        metrics=[],
        use_checkpoint=opt.ckpt,
    )
    opt_vid = opt
    trainer_vid = trainer
    model_vid = model


def video_process(opt, trainer, model, dir_path):
    test_loader = NeRFDataset_Test(opt, device="cuda").dataloader()
    # temp fix: for update_extra_states
    model.aud_features = test_loader._data.auds
    model.eye_areas = test_loader._data.eye_area
    global fd_v, v_access
    v_access = False
    Thread(target=write_audio, args=(dir_path["audio"],)).start()
    try:
        _ = sync_q.get(timeout=1)
    except Empty:
        ...
    trainer.test(
        test_loader,
        name=dir_path["input"].split("/")[-1].split(".")[0],
        fd_pipe=fd_v,
    )
    v_access = True
    return dir_path["output"]


def audio_process(audio_path):
    with ASR(opt_au, processor_au, model_au, audio_path) as asr:
        asr.run()


def generate_video(audio_path, audio_path_eo, video_path, output_path):
    opt_vid.aud = audio_path_eo
    dir_path = {
        "audio": audio_path,
        "input": video_path,
        "output": output_path,
    }
    path = video_process(opt_vid, trainer_vid, model_vid, dir_path)
    return path


'''持续写入44100/fps长度的0音频数组以及图像矩阵'''


def ffmpeg_pre_process():
    make_pipe()
    _ = run_ffmpeg()
    Thread(target=write_video_idle).start()
    Thread(target=write_audio_idle).start()


def write_video_idle():
    global fd_v, v_access, frame_sec, write_v_f, write_a_f
    if fd_v is None:
        fd_v = os.open(video_pipe_name, os.O_WRONLY)
    while True:
        if v_access is False:
            if write_a_f > write_v_f:
                for _ in range(write_a_f - write_v_f):
                    os.write(fd_v, v_full_idle.tobytes())
                    write_v_f += 1
                try:
                    sync_q.put(item=True, block=False)
                except Full:
                    continue
                print("write_a_f=%d write_v_f=%d\n" % (write_a_f, write_v_f))
            time.sleep(frame_sec)
        else:
            t0 = time.time()
            for _ in range(1, fps):
                if v_access is False:
                    break
                os.write(fd_v, v_full_idle.tobytes())
                write_v_f += 1
            try:
                time.sleep(1 - time.time() + t0)
            except ValueError:
                ...
            if write_v_f > write_a_f:
                time.sleep(frame_sec)


def write_audio_idle():
    global fd_a, v_access, frame_sec, write_v_f, write_a_f
    if fd_a is None:
        fd_a = os.open(audio_pipe_name, os.O_WRONLY)
    while True:
        if v_access is False:
            if write_v_f > write_a_f:
                for _ in range(write_v_f - write_a_f):
                    os.write(fd_a, a_full_idle.tobytes())
                    write_a_f += 1
                try:
                    sync_q.put(item=True, block=False)
                except Full:
                    continue
                print("write_a_f=%d write_v_f=%d\n" % (write_a_f, write_v_f))
            time.sleep(frame_sec)
        else:
            t0 = time.time()
            for _ in range(fps):
                if v_access is False:
                    break
                os.write(fd_a, a_full_idle.tobytes())
                write_a_f += 1
            try:
                time.sleep(1 - time.time() + t0)
            except ValueError:
                ...
            if write_a_f > write_v_f:
                time.sleep(frame_sec)


def make_pipe():
    # 判断如果管道存在，则先unlink
    if os.path.exists(video_pipe_name):
        os.unlink(video_pipe_name)
    if os.path.exists(audio_pipe_name):
        os.unlink(audio_pipe_name)
    os.mkfifo(video_pipe_name)
    os.mkfifo(audio_pipe_name)


def run_ffmpeg() -> subprocess.Popen:
    proc = subprocess.Popen(command, shell=False, stdin=subprocess.PIPE)
    # print("ffmpeg exit, exit code = %d" % (proc.wait()))
    return proc


def write_audio(audio_path):
    speech_array, _ = librosa.load(audio_path, sr=44100)
    speech_array = (speech_array * 32767).astype(np.int16)
    global fd_a
    wav_frame_num = int(44100 / fps)
    frame_counter = 0
    while True:
        # 由于音频流的采样率是xxx, 而视频流的帧率是25, 因此需要对音频流进行分帧
        speech = speech_array[
                 frame_counter * wav_frame_num: (frame_counter + 1) * wav_frame_num
                 ]
        os.write(fd_a, speech.tobytes())
        frame_counter += 1
        if frame_counter * wav_frame_num >= len(speech_array):
            break
