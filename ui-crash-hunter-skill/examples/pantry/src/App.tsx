import { useEffect, useState } from 'react';
import { Header } from './components/Header';
import { RouteBoundary } from './components/RouteBoundary';
import { match } from './routes';

export function App() {
  const [hash, setHash] = useState(window.location.hash || '#/');

  useEffect(() => {
    const onChange = () => setHash(window.location.hash || '#/');
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const { route, params } = match(hash.slice(1));
  const Screen = route.component;

  return (
    <>
      <Header />
      <main>
        <RouteBoundary key={hash}>
          <Screen params={params} />
        </RouteBoundary>
      </main>
    </>
  );
}
