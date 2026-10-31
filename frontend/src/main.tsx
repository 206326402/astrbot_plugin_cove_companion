import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import recordRoomCss from './style.css?inline';
import interCss from './inter.css?inline';
import room13Css from './room-13.css?inline';

// AstrBot authenticates the entry asset; keep styles in that same payload.
const style = document.createElement('style');
style.dataset.coveRecordRoom = '1.1';
style.textContent = interCss + recordRoomCss + room13Css;
document.head.appendChild(style);

class ErrorBoundary extends React.Component<React.PropsWithChildren, {error: string}> {
  state = {error: ''};
  static getDerivedStateFromError(error: unknown) {
    return {error: error instanceof Error ? error.message : String(error)};
  }
  render() {
    if (this.state.error) {
      return <div className="app"><section className="panel content-panel"><div className="section-title">页面渲染失败</div><h2>控制台遇到异常</h2><p className="muted">插件后台仍可能继续运行。请刷新页面；如果问题重复出现，请导出诊断信息。</p><pre>{this.state.error}</pre><button onClick={() => window.location.reload()}>重新加载</button></section></div>;
    }
    return this.props.children;
  }
}

createRoot(document.getElementById('root')!).render(<ErrorBoundary><App /></ErrorBoundary>);
