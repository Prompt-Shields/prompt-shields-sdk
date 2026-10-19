# Prompt Shields gateway image (ghcr.io/prompt-shields/prompt-shields-sdk/gateway).
#
# Runtime contract (Kubernetes restricted Pod Security Standard):
# - runs as UID/GID 10001; every file is root-owned and world-readable, so an
#   arbitrary UID (OpenShift) works too;
# - runs node directly (PID 1 gets SIGTERM and drains, see src/psShutdown.ts);
# - works with a read-only root filesystem. Nothing is written at runtime
#   unless you opt into the file cache backend; mount an emptyDir at /tmp if
#   a dependency needs scratch space.

FROM node:20-alpine AS build

WORKDIR /app

# Upgrade system packages
RUN apk upgrade --no-cache

# Upgrade npm to version 10.9.2
RUN npm install -g npm@10.9.2

# husky (the `prepare` script) is a git hook installer: not needed in an image.
ENV HUSKY=0

# Install app dependencies (postinstall applies patches/ with patch-package)
COPY package*.json ./
COPY patches ./patches
RUN npm ci

# Copy the rest of the application code, build, then keep production
# dependencies only
COPY . .
RUN npm run build \
&& rm -rf node_modules \
&& npm ci --omit=dev \
&& npm cache clean --force

FROM node:20-alpine

# Upgrade system packages. npm, npx, corepack and yarn are not needed to run
# the gateway (and their bundled dependencies only add CVEs to scan).
RUN apk upgrade --no-cache \
&& rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
  /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
  /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg

WORKDIR /app

# Copy the build directory, node_modules, and package.json to the working directory
COPY --from=build /app/build /app/build
COPY --from=build /app/node_modules /app/node_modules
COPY --from=build /app/package.json /app/package.json

USER 10001:10001

# Expose port 8787
EXPOSE 8787

# --headless: no /public console UI (it is also off when NODE_ENV or
# ENVIRONMENT is "production"). Pass other arguments to override, e.g.
# `docker run <image> --port=8787` to serve the console.
ENTRYPOINT ["node", "build/start-server.js"]
CMD ["--headless"]
