ARG BT_IMAGE=btpanel/baota:lnmp
FROM ${BT_IMAGE}

# 安装 entrypoint 与 loopback API 转发器
COPY entrypoint.sh /entrypoint.sh
COPY forward.py /forward.py
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
