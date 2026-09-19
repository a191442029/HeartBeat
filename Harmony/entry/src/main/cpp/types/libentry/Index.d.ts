
export const setFilesDir: (dir: string) => void;
export const tailscaleInit: (controlUrl: string, authKey: string, keyFilePath: string) => Promise<string>;
export const tailscaleGetPeerIpv4: (peerName: string) => Promise<string>;
export const tailscaleDeinit: () => void;
export const tailscaleStatus: () => string;
export const tailscaleTcpTest: (host: string, port: number) => Promise<string>;
export const sshConnect: (host: string, port: number, username: string, password: string) => Promise<string>;
export const sshRead: () => Promise<string>;
export const sshWrite: (data: string) => Promise<void>;
export const sshClose: () => void;
export const vpnBridge: (tunFd: number) => Promise<number>;
export const vpnDiag: (host: string, port: number) => Promise<string>;
export const vpnLatencyTest: (host: string, port: number, count: number) => Promise<string>;
export const getRustLog: () => string;
export const getPeers: () => string;
