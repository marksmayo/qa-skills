import { Component, type ReactNode } from 'react';

/** Wraps optional panels: a render error hides the panel only. */
export class PanelBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? <p className="panel-unavailable">Not available right now.</p> : this.props.children;
  }
}
