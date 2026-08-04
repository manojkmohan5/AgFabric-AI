# Multi-stage so the shipped image holds only the pruned standalone server,
# never the full workspace, dev dependencies, or the npm cache.
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Next.js inlines NEXT_PUBLIC_* at build time, not at container start, so it
# has to arrive as a build argument here rather than a runtime environment
# variable on the eventual container.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
# This project ships no public/ directory (no next/image usage, nothing
# static to serve), but the runtime stage always copies one — mkdir -p makes
# that copy succeed as an empty directory instead of failing the build, and
# costs nothing if a public/ directory is added later.
RUN mkdir -p public && npm run build

FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
# output: "standalone" in next.config.ts traces the production dependency
# graph, so these three paths are the entire runtime — no node_modules install
# needed in this stage.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
