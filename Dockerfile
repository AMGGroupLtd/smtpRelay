
# Use the original image as the base
FROM ghcr.io/justiniven/smtp-oauth-relay:latest

# Switch to root so we can install packages
USER root

# Install additional packages here
# (example shown: curl, nano – replace with whatever you need)
RUN apt-get update
RUN apt-get install -y --no-install-recommends
RUN apt-get install -y --no-install-recommends curl
RUN apt-get install -y --no-install-recommends ssmtp
RUN apt-get clean
RUN rm -rf /var/lib/apt/lists/*
COPY configSsmtp /usr/local/bin/


