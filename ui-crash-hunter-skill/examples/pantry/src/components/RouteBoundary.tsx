import { Component, type ReactNode } from 'react';

interface State {
  error: Error | null;
}

/** Wraps each screen: a render error replaces the screen, the header stays. */
export class RouteBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error('[RouteBoundary]', error);
  }

  render() {
    if (this.state.error) {
      return (
        <div role="alert">
          <h2>Something went wrong</h2>
          <p>This page couldn't be shown.</p>
          <a href="#/">Back to recipes</a>
        </div>
      );
    }
    return this.props.children;
  }
}
