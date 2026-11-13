export interface PermissionRequest {
  tool: string;
  path?: string;
}

/** Answers the agent's permission requests during a session. */
export function onPermissionRequest(request: PermissionRequest): 'allow' | 'deny' {
  return request.tool === 'read' ? 'allow' : 'deny';
}
