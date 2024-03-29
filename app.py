# server.py
import asyncio
import json
import time

import edge_tts
from flask import Flask
from flask_socketio import SocketIO

from tools import audio_pre_process, video_pre_process, generate_video, audio_process

app = Flask(__name__)
socketio = SocketIO(app)


async def main(voicename: str, text: str, OUTPUT_FILE):
    communicate = edge_tts.Communicate(text, voicename, receive_timeout=20)

    with open(OUTPUT_FILE, "wb") as file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                pass


def send_information(path=None):
    data = {}
    if path == None:
        data = {
            "video": None,
        }
    else:
        data = {
            "video": path,
        }
    json_data = json.dumps(data)
    socketio.send(json_data)


def txt_to_audio(text_):
    cur_time = round(time.time() * 1000)
    audio_path = "data/audio/aud_{}.wav".format(cur_time)
    audio_path_eo = "data/audio/aud_{}_eo.npy".format(cur_time)
    video_path = "data/video/results/ngp_{}.mp4".format(cur_time)
    output_path = "data/video/results/output_{}.mp4".format(cur_time)
    voicename = "zh-CN-YunjianNeural"
    # 让我们一起学习。必应由 AI 提供支持，因此可能出现意外和错误。请确保核对事实，并 共享反馈以便我们可以学习和改进!
    text = text_
    record_time = time.time()
    with open("data/video/log_video_gen.txt", mode="a") as f:
        asyncio.get_event_loop().run_until_complete(main(voicename, text, audio_path))
        cur_time = time.time()
        print(
            "------生成音频所需时间:{}------".format(cur_time - record_time),
            file=f,
            flush=True,
        )
        record_time = time.time()
        audio_process(audio_path)
        cur_time = time.time()
        print(
            "------处理音频所需时间:{}------".format(cur_time - record_time),
            file=f,
            flush=True,
        )

    return audio_path, audio_path_eo, video_path, output_path


@socketio.on("connect")
def test_connect(auth):
    print("Client connected")


@socketio.on("disconnect")
def test_disconnect():
    print("Client disconnected")


@socketio.on("dighuman")
def dighuman(dighuman):
    with open("data/video/log_video_gen.txt", mode="a") as f:
        dighuman = dighuman.replace(" ", "")
        if len(dighuman) == 0:
            return
        audio_path, audio_path_eo, video_path, output_path = txt_to_audio(dighuman)
        generate_video(audio_path, audio_path_eo, video_path, output_path)
        send_information(output_path)
    send_information()


if __name__ == "__main__":
    audio_pre_process()
    video_pre_process()

    socketio.run(app, port=8800)
