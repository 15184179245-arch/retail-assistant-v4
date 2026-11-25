import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorPanel } from "../components/ErrorPanel";

export interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: (error: Error, reset: () => void) => ReactNode;
  onReset?: () => void;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

/**
 * Page-level error boundary.
 * Isolates runtime rendering exceptions to prevent white-screen crashes,
 * keeping the top navigation and layout shell interactive.
 * Never leaks raw JavaScript stack traces to the end user.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  public override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Invoke optional error callback for telemetry/logging
    this.props.onError?.(error, errorInfo);
  }

  public reset = (): void => {
    this.setState({ hasError: false, error: null });
    this.props.onReset?.();
  };

  public override render(): ReactNode {
    if (this.state.hasError && this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.reset);
      }

      return (
        <div className="error-boundary-container" style={{ padding: "24px 0" }}>
          <ErrorPanel
            userMessage="页面组件加载或渲染异常，已为您隔离当前视图。"
            hint="您可以点击下方按钮重新加载当前页面，或通过上方导航切换至其他业务功能。"
            code="RENDER_CRASH"
            onRetry={this.reset}
            retryLabel="重新加载视图"
          />
        </div>
      );
    }

    return this.props.children;
  }
}
