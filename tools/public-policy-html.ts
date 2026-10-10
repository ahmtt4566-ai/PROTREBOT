import type {Plugin} from 'vite'
import {PUBLIC_PAGE_METADATA, publicPageForPath, publicPageHeadValues, publicPageSchema, type PublicPage} from '../public-page-metadata'

function escapeAttribute(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export function renderPublicPageHtml(html: string, page: PublicPage): string {
  const replaceOne = (pattern: RegExp, replacement: string) => {
    if (Array.from(html.matchAll(pattern)).length !== 1) throw new Error(`Expected one public page head element: ${pattern}`)
    html = html.replace(pattern, () => replacement)
  }
  replaceOne(/<title>[^<]*<\/title>/g, `<title>${escapeAttribute(PUBLIC_PAGE_METADATA[page].title)}</title>`)
  for (const [selector, attribute, value] of publicPageHeadValues(page)) {
    const match = /^(meta|link)\[(name|property|rel)="([^"]+)"\]$/.exec(selector)
    if (!match) throw new Error(`Unsupported metadata selector: ${selector}`)
    const [, tag, key, name] = match
    replaceOne(new RegExp(`<${tag}\\b[^>]*\\b${key}="${name}"[^>]*>`, 'g'),
      `<${tag} ${key}="${name}" ${attribute}="${escapeAttribute(value)}"/>`)
  }
  replaceOne(/<script\b[^>]*type="application\/ld\+json"[^>]*>[\s\S]*?<\/script>/g,
    `<script type="application/ld+json">${JSON.stringify(publicPageSchema(page))}</script>`)
  return html
}

export function publicPolicyHtml(): Plugin {
  return {
    name: 'kaistrade-public-policy-html',
    enforce: 'post',
    configurePreviewServer(server) {
      server.middlewares.use((request, _response, next) => {
        const url = request.url || '/'
        const page = publicPageForPath(url)
        if (page && page !== 'home') {
          const query = url.indexOf('?')
          request.url = `/${page}.html${query < 0 ? '' : url.slice(query)}`
        }
        next()
      })
    },
    transformIndexHtml(html, context) {
      if (!context.server) return html
      const page = publicPageForPath(context.originalUrl || context.path)
      return page ? renderPublicPageHtml(html, page) : html
    },
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const entry = bundle['index.html']
        if (!entry || entry.type !== 'asset' || typeof entry.source !== 'string') throw new Error('Public page build requires index.html')
        for (const page of ['privacy', 'terms', 'risk'] as const) {
          this.emitFile({type: 'asset', fileName: `${page}.html`, source: renderPublicPageHtml(entry.source, page)})
        }
      },
    },
  }
}
