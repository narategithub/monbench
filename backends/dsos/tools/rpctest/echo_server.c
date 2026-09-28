/* Server procedure for echo.x: return the received buffer unchanged. */
#include "echo.h"

echo_buf *echo_1_svc(echo_buf *arg, struct svc_req *req)
{
	static echo_buf result;
	(void)req;
	/* The dispatcher sends the reply before it frees the argument. */
	result = *arg;
	return &result;
}
