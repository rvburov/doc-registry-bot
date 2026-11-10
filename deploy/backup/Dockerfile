# Образ ночной копии: pg_dump из образа PostgreSQL и smbclient для сетевой папки Windows.
FROM postgres:16
RUN apt-get update \
 && apt-get install -y --no-install-recommends smbclient \
 && rm -rf /var/lib/apt/lists/*
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod 0755 /usr/local/bin/backup.sh
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["loop"]
