FROM ctfd/ctfd:3.8.5

ENV PATH="/opt/venv/bin:$PATH"
COPY ./plugin /opt/CTFd/CTFd/plugins/lab_manager

USER 1001
EXPOSE 8000
ENTRYPOINT ["/opt/CTFd/docker-entrypoint.sh"]