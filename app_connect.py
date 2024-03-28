from app import app, socketio
from tools import audio_pre_process, video_pre_process, ffmpeg_pre_process
import json

if __name__ == "__main__":
    audio_pre_process()
    video_pre_process()
    ffmpeg_pre_process()
    # 运行测试客户端
    test_client = socketio.test_client(app)
    test_client.connect()
    while True:
        t = input("生成视频的一段话:")
        test_client.emit("dighuman", t)
        mes = []
        while True:
            mes.extend(test_client.get_received())
            json_data = mes[-1]["args"]
            mapping = json.loads(json_data)
            if mapping["video"] is None:
                break
        # while len(mes) != 0:
