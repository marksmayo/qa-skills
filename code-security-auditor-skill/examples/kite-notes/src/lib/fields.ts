// Field names, not values: the secret scanner must not read the next element as a value.
export const ENCRYPTED_FIELDS = {
  User: ['email', 'resetToken', 'apiKeyHint'],
};

export const CHECKOUT_SCHEMA = {
  required: ['clientSecret', 'paymentIntentId'],
};
