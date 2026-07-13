FROM node:22-alpine AS build

WORKDIR /app

RUN mkdir -p /app/build
COPY miniapp/index.html /app/build/index.html

FROM nginx:alpine
COPY miniapp/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html

EXPOSE 80

