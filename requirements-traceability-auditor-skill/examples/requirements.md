# Loyalty programme: non-functional requirements

Version 1.2, agreed with the product owner on 2026-10-30.

## REQ-NFR-1 Points calculation time

Calculating the points for an order takes under 50 ms at the 95th percentile, measured at the loyalty
service, for orders of up to 100 lines.

## REQ-NFR-2 Points history retention

A member's points history is kept for 7 years after their last order.
