import { NextResponse } from 'next/server';

export function middleware() {
  const res = NextResponse.next();
  res.headers.set('X-Robots-Tag', 'all');
  return res;
}

export const config = { matcher: ['/((?!_next/static|favicon.ico).*)'] };
