"""本地端口转发：让插件经 loopback 访问 mock-api

API 客户端强制非 loopback 地址走 HTTPS 且拦截内网 IP（SSRF 防护），
容器内以 127.0.0.1 转发到 mock-api 服务，即可用 HTTP 完成集成测试。
仅标准库，无第三方依赖。
"""
import socket
import sys
import threading


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client, remote_host, remote_port):
    try:
        remote = socket.create_connection((remote_host, remote_port), timeout=10)
    except OSError:
        client.close()
        return
    threading.Thread(target=_pipe, args=(client, remote), daemon=True).start()
    threading.Thread(target=_pipe, args=(remote, client), daemon=True).start()


def main():
    if len(sys.argv) != 5:
        print('用法: forward.py <bind_host> <bind_port> <remote_host> <remote_port>')
        sys.exit(1)
    bind_host, bind_port = sys.argv[1], int(sys.argv[2])
    remote_host, remote_port = sys.argv[3], int(sys.argv[4])

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_host, bind_port))
    srv.listen(64)
    while True:
        client, _ = srv.accept()
        _handle(client, remote_host, remote_port)


if __name__ == '__main__':
    main()
