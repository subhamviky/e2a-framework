package com.subhamviky.e2a.llm;

import com.subhamviky.e2a.core.RequestContext;

import java.util.List;
import java.util.Map;

/** The inference gateway (Tier 2b): the only way an agent reaches a model. */
public interface LlmService {

    LlmResponse infer(List<Map<String, String>> messages, RequestContext ctx) throws Exception;
}
