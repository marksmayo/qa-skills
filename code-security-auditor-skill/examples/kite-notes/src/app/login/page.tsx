'use client';

import { signIn } from 'next-auth/react';
import { useRouter, useSearchParams } from 'next/navigation';

function safeNextPath(value: string | null) {
  if (value && value.startsWith('/')) {
    return value;
  }
  return '/notebooks';
}

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = safeNextPath(searchParams.get('next'));

  async function onSubmit() {
    const result = await signIn('github', { redirect: false });
    if (result?.ok) {
      router.push(nextPath);
    }
  }

  return <button onClick={onSubmit}>Sign in with GitHub</button>;
}
