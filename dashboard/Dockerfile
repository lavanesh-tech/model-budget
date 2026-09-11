# syntax=docker/dockerfile:1.7
#
# Multi-stage build. Relies on next.config.ts's `output: 'standalone'`
# (Step 39 addition -- see that file) to produce a minimal self-
# contained server bundle, so the runtime stage never ships full
# node_modules.
#
# No secret is ever a build ARG or baked into any layer: every value
# this app reads (BACKEND_ADMIN_BASE_URL, MODEL_BUDGET_ADMIN_API_KEY,
# DASHBOARD_SESSION_SECRET, DASHBOARD_LOGIN_PASSWORD, DASHBOARD_ORIGIN)
# is read at REQUEST time inside Node-runtime Route Handlers (see
# lib/security.mjs / lib/session.ts) -- never at build time, never in
# a "use client" file, never in a NEXT_PUBLIC_ variable. `npm run
# build` here runs with NO backend/session secrets present at all.

FROM node:22.11.0-bookworm-slim AS base
ENV NEXT_TELEMETRY_DISABLED=1

FROM base AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0
RUN groupadd --system --gid 1001 nextjs \
    && useradd --system --uid 1001 --gid nextjs --no-create-home --shell /usr/sbin/nologin nextjs

# NOTE (unverified against the real repo -- confirm before first
# build): this assumes dashboard/public/ exists, as it does in a
# standard Next.js App Router project. If it does not exist in this
# repository, delete the next COPY line -- Docker's COPY fails the
# build entirely on a missing source path, it does not skip silently.
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

# Probes the new, unauthenticated app/api/health/route.ts (Step 39
# addition) -- never a session-gated route, so the healthcheck itself
# never needs (and never gets) a credential.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["node", "-e", "require('http').get('http://127.0.0.1:3000/api/health',res=>{process.exit(res.statusCode===200?0:1)}).on('error',()=>process.exit(1))"]

CMD ["node", "server.js"]
