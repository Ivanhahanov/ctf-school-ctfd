FROM ctfd/ctfd:3.8.5

ENV PATH="/opt/venv/bin:$PATH"
COPY ./plugin /opt/CTFd/CTFd/plugins/lab_manager
COPY ./plugin-tournaments /opt/CTFd/CTFd/plugins/hidden_tournaments
COPY ./theme/alfa /opt/CTFd/CTFd/themes/alfa

USER 1001
EXPOSE 8000
ENTRYPOINT ["/opt/CTFd/docker-entrypoint.sh"]