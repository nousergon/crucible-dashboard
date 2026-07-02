// Host-based canonical redirect (Brian, 2026-07-02): fitness.nousergon.ai is
// attached to this Pages project as a second custom domain — the fitness
// *category* URL routes to the single fitness product for now — but the
// canonical product URL is vires.nousergon.ai. 301 any request arriving on a
// non-canonical host to the same path+query on the canonical host, so users
// and crawlers always end up on one URL. A zone-level Redirect Rule would also
// work, but this lives in the repo (version-controlled, auditable) instead of
// as dashboard config that can drift silently.
//
// When a real fitness category page/site exists: detach fitness.nousergon.ai
// from this project, point it at the new site, and delete this middleware
// entry for the host.
//
// pages.dev hosts (production alias + preview deployments) are NOT redirected,
// so the deploy smoke probe and preview links keep working.

const CANONICAL_HOST = "vires.nousergon.ai";
const REDIRECT_HOSTS = new Set(["fitness.nousergon.ai"]);

interface MiddlewareContext {
  request: Request;
  next: () => Promise<Response>;
}

export async function onRequest(context: MiddlewareContext): Promise<Response> {
  const url = new URL(context.request.url);
  if (REDIRECT_HOSTS.has(url.hostname)) {
    url.hostname = CANONICAL_HOST;
    return Response.redirect(url.toString(), 301);
  }
  return context.next();
}
