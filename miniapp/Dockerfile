FROM node:22-alpine AS build

WORKDIR /app

COPY miniapp/package.json miniapp/package-lock.json ./
RUN npm ci

COPY miniapp/ ./
RUN node node_modules/vite/bin/vite.js build

FROM nginx:alpine
COPY miniapp/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html

EXPOSE 80
