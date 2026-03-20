ARG BT_IMAGE=btpanel/baota:lnmp
FROM ${BT_IMAGE}

# 安装 entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
