package com.aiinvestment.portfolio.application;

import java.util.UUID;

/** Published after a portfolio/import path attaches an instrument row to its global identity. */
public record GlobalInstrumentAttachedEvent(UUID globalInstrumentId) {}
