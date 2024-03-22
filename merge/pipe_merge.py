import os
import subprocess
import threading

import cv2
import librosa
import numpy as np


# 将视频流写入管道
def write_video_stream(cap, fps, pipe_name):
    fd_pipe = os.open(pipe_name, os.O_WRONLY)
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        os.write(fd_pipe, frame.tobytes())
    os.close(fd_pipe)


# 将音频流写入管道;
def write_audio_stream(cap, speech_array, fps, pipe_name):
    fd_pipe = os.open(pipe_name, os.O_WRONLY)
    wav_frame_num = int(44100 / fps)
    frame_counter = 0
    while True:
        # 由于音频流的采样率是xxx, 而视频流的帧率是25, 因此需要对音频流进行分帧
        speech = speech_array[frame_counter * wav_frame_num: (frame_counter + 1) * wav_frame_num]
        os.write(fd_pipe, speech.tobytes())
        frame_counter += 1
        # 根据视频帧数决定音频写入次数
        if frame_counter == int(cap.get(cv2.CAP_PROP_FRAME_COUNT)):
            break
    os.close(fd_pipe)


def push():
    # 模拟数字人生成的视频流和音频流
    # 使用OpenCV读取视频流
    cap = cv2.VideoCapture("merge/output_1710743841513.mp4")
    # 使用librosa读取音频流
    speech_array, sr = librosa.load("merge/aud_1710743841513.wav", sr=44100)  # 对于rtmp, 音频速率是有要求的，这里采用了16000
    speech_array = (speech_array * 32767).astype(np.int16)  # 转为整型

    push_url = 'rtmp://127.0.0.1:1935/humanlive'

    # 获取视频流的帧率、宽度和高度
    fps = float(cap.get(5))
    width = int(cap.get(3))
    height = int(cap.get(4))

    # 创建两个"named pipes"，用于存放视频流和音频流
    # 判断如果管道存在，则先unlink
    if os.path.exists('video_pipe'):
        os.unlink('video_pipe')
    if os.path.exists('audio_pipe'):
        os.unlink('audio_pipe')
    os.mkfifo('video_pipe')
    os.mkfifo('audio_pipe')

    # ffmpeg命令，不做详解，可以参考ffmpeg文档
    command = ['ffmpeg',
               '-loglevel', 'info',
               '-y', '-an',
               '-f', 'rawvideo',
               '-vcodec', 'rawvideo',
               '-pix_fmt', 'bgr24',
               '-s', "{}x{}".format(width, height),
               '-r', str(fps),
               '-i', 'video_pipe',  # 视频流管道作为输入
               '-f', 's16le',
               '-acodec', 'pcm_s16le',
               '-i', 'audio_pipe',  # 音频流管道作为输入
               '-c:v', "libx264",
               '-pix_fmt', 'yuv420p',
               '-s', "512x512",
               '-preset', 'ultrafast',
               '-profile:v', 'baseline',
               '-tune', 'zerolatency',
               '-g', '2',
               '-b:v', "1000k",
               '-ac', '1',
               '-ar', '44100',
               '-acodec', 'aac',
               '-shortest',
               '-f', 'flv',
               push_url]
    # 启动进程运行ffmpeg命令
    proc = subprocess.Popen(command, shell=False, stdin=subprocess.PIPE)

    # 创建两个线程，分别将视频流和音频流写入"named pipes"
    video_thread = threading.Thread(target=write_video_stream, args=(cap, fps, 'video_pipe'))
    audio_thread = threading.Thread(target=write_audio_stream, args=(cap, speech_array, fps, 'audio_pipe'))

    video_thread.start()
    audio_thread.start()

    video_thread.join()
    audio_thread.join()

    proc.wait()

    # Remove the "named pipes".
    os.unlink('video_pipe')
    os.unlink('audio_pipe')


if __name__ == "__main__":
    push()
