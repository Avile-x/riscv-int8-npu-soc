`ifndef NPU_TYPES_SV
`define NPU_TYPES_SV

typedef logic signed [7:0]  int8_t;
typedef logic signed [15:0] product_t;
typedef logic signed [31:0] int32_t;

typedef int8_t  matrix8_t  [0:3][0:3];
typedef int32_t matrix32_t [0:3][0:3];

typedef enum logic [2:0] {
    IDLE,
    LOAD_WEIGHTS,
    COMPUTE,
    DRAIN,
    HOLD_RESULT
} array_state_t;

`endif
