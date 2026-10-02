FROM ubuntu:latest
LABEL authors="raphaelwunderlin"

ENTRYPOINT ["top", "-b"]