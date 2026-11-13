package com.subhamviky.e2a.llm;

/** A model answer: the text and the tokens it cost. */
public record LlmResponse(String text, int tokensUsed) {
}
