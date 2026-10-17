# The "Vercel" side: a Next.js server with pi inside. No shell tools are used from here.
FROM node:24.21.0-trixie-slim
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npx next build && chown -R node:node /app
USER node
ENV NODE_ENV=production
EXPOSE 3100
CMD ["node", "node_modules/next/dist/bin/next", "start", "-p", "3100"]
