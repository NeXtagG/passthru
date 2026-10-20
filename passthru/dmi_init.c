/* Tiny /init for the SMBIOS self-test guest: prints the DMI entry point and
 * table as hex on the serial console, then powers off. Built static. */
#include <fcntl.h>
#include <stdio.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <termios.h>
#include <unistd.h>

static void dump(const char *tag, const char *path)
{
    unsigned char buf[4096];
    int fd = open(path, O_RDONLY);
    ssize_t n;
    printf("\n@@%s@@\n", tag);
    if (fd < 0) {
        printf("missing\n");
    } else {
        while ((n = read(fd, buf, sizeof buf)) > 0)
            for (ssize_t i = 0; i < n; i++)
                printf("%02x", buf[i]);
        close(fd);
    }
    printf("\n@@end@@\n");
}

int main(void)
{
    mount("sysfs", "/sys", "sysfs", 0, NULL);
    dump("entry", "/sys/firmware/dmi/tables/smbios_entry_point");
    dump("table", "/sys/firmware/dmi/tables/DMI");
    fflush(stdout);
    tcdrain(1);  /* let the UART finish before power-off cuts it */
    sleep(1);
    sync();
    reboot(RB_POWER_OFF);
    return 0;
}
