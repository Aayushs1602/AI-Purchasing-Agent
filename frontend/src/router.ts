import { useEffect, useState } from 'react'

// Tiny hash router: #/ (inbox), #/runs/RUN-123, #/approvals. No dependency needed.
export type Route =
  | { page: 'inbox' }
  | { page: 'run'; id: string }
  | { page: 'approvals' }

function parse(hash: string): Route {
  const path = hash.replace(/^#/, '') || '/'
  const run = path.match(/^\/runs\/([\w-]+)$/)
  if (run) return { page: 'run', id: run[1] }
  if (path === '/approvals') return { page: 'approvals' }
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
