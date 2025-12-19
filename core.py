import threading
import queue
import time
import uuid
import json
import requests
from flask import Flask, Response, request, stream_with_context

app = Flask(__name__)

# --- 全局配置 ---
BUFFER_SIZE = 10 * 1024 * 1024  # 缓冲区大小 10MB
CHUNK_SIZE = 8192               # 单次读取字节
RETRY_DELAY = 1                 # 重连延迟(秒)
READ_TIMEOUT = 10               # 读取超时时间(秒)

# --- 全局状态管理 ---
active_streams = {}
streams_lock = threading.Lock()

class StreamBuffer:
    def __init__(self, source_url):
        self.id = uuid.uuid4().hex[:8]
        self.source_url = source_url
        self.q = queue.Queue(maxsize=BUFFER_SIZE // CHUNK_SIZE)
        self.running = True
        
        # 统计指标
        self.start_time = time.time()
        self.last_active = time.time()
        self.reconnect_count = 0
        self.total_bytes = 0
        self.state = "INIT"  # INIT, CONNECTING, STREAMING, RECONNECTING, STOPPED
        
        # 启动后台下载线程
        self.thread = threading.Thread(target=self._download_loop, name=f"Downloader-{self.id}")
        self.thread.daemon = True
        self.thread.start()

    def _download_loop(self):
        print(f"[{self.id}] 启动代理: {self.source_url}")
        
        while self.running:
            try:
                self.state = "CONNECTING"
                # stream=True 建立流式连接
                with requests.get(self.source_url, stream=True, timeout=READ_TIMEOUT) as r:
                    r.raise_for_status()
                    self.state = "STREAMING"
                    print(f"[{self.id}] 源已连接")
                    
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if not self.running:
                            break
                        if chunk:
                            # 写入队列（如果满了会阻塞，起到背压作用）
                            self.q.put(chunk)
                            self.total_bytes += len(chunk)
                            self.last_active = time.time()
            except Exception as e:
                if self.running:
                    self.state = "RECONNECTING"
                    self.reconnect_count += 1
                    # print(f"[{self.id}] 连接中断，正在重连... ({e})") 
                    time.sleep(RETRY_DELAY)
        
        self.state = "STOPPED"
        print(f"[{self.id}] 代理线程结束")

    def stop(self):
        self.running = False
        # 塞入 None 解除消费者的阻塞
        try:
            self.q.put(None, block=False)
        except:
            pass

    def generate(self):
        """生成器：供 Flask 响应使用"""
        try:
            while self.running:
                try:
                    # 从缓冲区取数据
                    chunk = self.q.get(timeout=5) 
                except queue.Empty:
                    # 如果缓冲区空了，但应该还在运行，说明在重连，继续等待
                    if not self.running:
                        break
                    continue
                
                if chunk is None:
                    break
                    
                yield chunk
        except GeneratorExit:
            # 客户端（播放器）断开连接
            pass
        except Exception as e:
            print(f"[{self.id}] 客户端推流异常: {e}")
        finally:
            self.stop()

    def get_status(self):
        uptime = int(time.time() - self.start_time)
        # 计算缓冲区占用率
        buffer_usage = 0
        if self.q.maxsize > 0:
            buffer_usage = round((self.q.qsize() / self.q.maxsize) * 100, 1)
        
        return {
            "id": self.id,
            "url": self.source_url,
            "state": self.state,
            "uptime_seconds": uptime,
            "reconnect_count": self.reconnect_count,
            "buffer_usage_percent": buffer_usage,
            "total_mb": round(self.total_bytes / (1024 * 1024), 2),
            "last_active_ago": int(time.time() - self.last_active)
        }

# --- 路由定义 ---

@app.route('/status')
def status_page():
    """查看所有流状态"""
    status_list = []
    with streams_lock:
        current_streams = list(active_streams.values())
    
    for stream in current_streams:
        status_list.append(stream.get_status())
    
    response_data = {
        "active_connections": len(status_list),
        "streams": status_list
    }
    return Response(json.dumps(response_data, ensure_ascii=False, indent=2), mimetype='application/json')

@app.route('/live')
def live_proxy():
    """
    代理入口: /live?url=http://...
    """
    target_url = request.args.get('url')
    if not target_url:
        return "Missing 'url' parameter", 400

    streamer = StreamBuffer(target_url)
    
    # 注册到全局状态
    with streams_lock:
        active_streams[streamer.id] = streamer
    
    try:
        return Response(
            stream_with_context(streamer.generate()), 
            content_type='video/mp2t'
        )
    finally:
        # 清理逻辑：当客户端断开连接后执行
        with streams_lock:
            if streamer.id in active_streams:
                del active_streams[streamer.id]
        print(f"[{streamer.id}] 客户端已断开，资源清理完毕")

# --- 启动函数 (供 app.py 调用) ---
def start_server():
    # threaded=True 对于处理并发流至关重要
    app.run(host='0.0.0.0', port=5000, threaded=True)
