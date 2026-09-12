package com.aiinvestment.broker.runtime;

public class IBKRRuntimeLifecycleException extends RuntimeException {
    private final String code;
    public IBKRRuntimeLifecycleException(String code) { super("IBKR runtime lifecycle operation failed."); this.code = code; }
    public IBKRRuntimeLifecycleException(String code, Throwable cause) { super("IBKR runtime lifecycle operation failed.", cause); this.code = code; }
    public String code() { return code; }
}
