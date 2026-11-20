import React from 'react'
export default class ErrorBoundary extends React.Component {
  constructor(p) { super(p); this.state = { error: null } }
  static getDerivedStateFromError(error) { return { error } }
  componentDidUpdate(prev) { if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null }) }
  render() {
    if (!this.state.error) return this.props.children
    return <div className="status-banner"><span>This page could not be shown: {String(this.state.error.message || this.state.error)}. Other pages still work; reload to try again.</span></div>
  }
}
