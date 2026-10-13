# GAIA frontend — the R3F 3D landing (built static, served by nginx).
# Build from gaia/frontend/:  docker build -t gaia-frontend .
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
