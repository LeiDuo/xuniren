# server.py
import asyncio
import base64
import json
import re
import time

import edge_tts
from flask import Flask
from flask_socketio import SocketIO

from tools import audio_pre_process, video_pre_process, generate_video, audio_process

app = Flask(__name__)
socketio = SocketIO(app)
video_list = []


async def main(voicename: str, text: str, OUTPUT_FILE):
    communicate = edge_tts.Communicate(text, voicename)

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
        with open(path, "rb") as f:
            video_data = base64.b64encode(f.read()).decode()
        data = {
            "video": "data:video/mp4;base64,%s" % video_data,
        }
    json_data = json.dumps(data)
    socketio.send(json_data)


def txt_to_audio(text_):
    audio_list = []
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
    txt_line = re.split(r"[。！!]", dighuman)
    # txt_line = [dighuman]
    if txt_line[-1] == "":
        txt_line = txt_line[0:-2]
    with open("data/video/log_video_gen.txt", mode="a") as f:
        print(
            f"接收到[{dighuman[0:5]}]...消息,开始分{len(txt_line)}段生成数字人视频",
            file=f,
            flush=True,
        )
        for line in txt_line:
            line = line.replace(" ", "")
            if len(line) == 0:
                break
            cur_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            print(f"准备生成视频时间:{cur_time}", file=f, flush=True)
            audio_path, audio_path_eo, video_path, output_path = txt_to_audio(line)
            generate_video(audio_path, audio_path_eo, video_path, output_path)
            cur_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            print(f"完成生成视频时间:{cur_time}", file=f, flush=True)
            video_list.append(output_path)
            send_information(output_path)
    send_information()


if __name__ == "__main__":
    audio_pre_process()
    video_pre_process()

    socketio.run(app, port=8800)
