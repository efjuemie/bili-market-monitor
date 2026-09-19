FROM node:20-alpine AS build
ARG VITE_API_BASE_URL=/api/v1
WORKDIR /app
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ .
COPY VERSION /VERSION
RUN VITE_APP_VERSION="$(cat /VERSION)" VITE_API_BASE_URL="$VITE_API_BASE_URL" npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /usr/share/caddy
COPY frontend/Caddyfile /etc/caddy/Caddyfile
