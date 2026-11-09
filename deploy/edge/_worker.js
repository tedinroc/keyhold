// Keyhold's pages.dev edge (Cloudflare Pages, advanced mode). Every request is forwarded to the
// hosted Keyhold and its response streamed back unchanged, so the live pages' server-sent events
// work. The origin address and the shared key are Pages secrets (ORIGIN, EDGE_KEY), not in this
// file. The key lets the app trust X-Keyhold-Client-IP, the visitor's address as Cloudflare saw it,
// so per-address limits apply per visitor and not to Cloudflare's address (web/security.py).
const OWN = ['x-keyhold-edge-key', 'x-keyhold-client-ip'];

export default {
  async fetch(request, env) {
    const here = new URL(request.url);
    const origin = new URL(env.ORIGIN);
    const target = new URL(here.pathname + here.search, origin);

    const headers = new Headers(request.headers);
    for (const name of OWN) headers.delete(name); // a visitor cannot send these
    headers.delete('host');
    headers.set('x-keyhold-edge-key', env.EDGE_KEY);
    headers.set('x-keyhold-client-ip', request.headers.get('cf-connecting-ip') || '');
    headers.set('x-forwarded-host', here.host);

    const hasBody = !['GET', 'HEAD'].includes(request.method);
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      redirect: 'manual',
    });

    const out = new Headers(upstream.headers);
    const location = out.get('location');
    if (location) {
      const to = new URL(location, target);
      if (to.host === origin.host) out.set('location', here.origin + to.pathname + to.search + to.hash);
    }
    return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
  },
};
