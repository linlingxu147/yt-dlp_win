# -*- coding: utf-8 -*-
import os
import sys
import webview

APP_DIR = os.path.dirname(os.path.abspath(__file__))


class Api:
    def ping(self):
        return "pong"


def main():
    api = Api()
    window = webview.create_window(
        "yt-dlp 下载器   ·   可视化界面",
        url=os.path.join(APP_DIR, "ui", "index.html"),
        js_api=api, width=1100, height=900, min_size=(920, 700))
    webview.start(http_server=True, debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()