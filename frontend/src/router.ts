import { useEffect, useState } from 'react'

// Tiny hash router: #/ (inbox), #/runs/RUN-123, #/approvals, #/new (or #/new/REC-C001 to
// start from a custom scenario). No dependency needed.
export type Route =
  | { page: 'inbox' }
  | { page: 'run'; id: string }
  | { page: 'approvals' }
  | { page: 'new'; from?: string }

function parse(hash: string): Route {
  const path = hash.replace(/^#/, '') || '/'
  const run = path.match(/^\/runs\/([\w-]+)$/)
  if (run) return { page: 'run', id: run[1] }
  if (path === '/approvals') return { page: 'approvals' }
  if (path === '/new') return { page: 'new' }
  const copy = path.match(/^\/new\/([\w-]+)$/)
  if (copy) return { page: 'new', from: copy[1] }
  return { page: 'inbox' }
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(window.location.hash))
  useEffect(() => {
    const onChange = () => setRoute(parse(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export const go = (path: string) => {
  window.location.hash = path
}
